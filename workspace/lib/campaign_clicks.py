"""Durable WhatsApp CTA destinations and aggregate click counts."""

from contextlib import closing
import os
from pathlib import Path
import re
import secrets
import sqlite3
from urllib.parse import urlsplit


BASE_URL_VAR = "GENAIE_CAMPAIGN_CLICK_BASE_URL"
LINK_ID = re.compile(r"[a-f0-9]{32}")
WHATSAPP_URL = re.compile(r"https://wa\.me/[0-9]{7,15}\?text=(?:[A-Za-z0-9_.~-]|%[0-9A-Fa-f]{2})*")
PREVIEW_AGENT = re.compile(
    r"\b(?:facebookexternalhit|facebot|meta-externalagent|meta-externalfetcher|"
    r"slackbot|discordbot|telegrambot|twitterbot|linkedinbot|googlebot|bingbot)\b"
    r"|^whatsapp/", re.IGNORECASE,
)
PREFETCH = re.compile(r"\b(?:prefetch|prerender)\b", re.IGNORECASE)
SCHEMA = """
CREATE TABLE IF NOT EXISTS click_groups (
    campaign TEXT NOT NULL,
    jid TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (campaign, jid)
);
CREATE TABLE IF NOT EXISTS click_links (
    id TEXT PRIMARY KEY,
    campaign TEXT NOT NULL,
    jid TEXT NOT NULL,
    destination TEXT NOT NULL,
    clicks INTEGER NOT NULL DEFAULT 0,
    UNIQUE (campaign, jid, destination)
);
"""


def base_url(env=None):
    env = os.environ if env is None else env
    value = (env.get(BASE_URL_VAR) or "").strip()
    if not value:
        return None
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname
            or not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?", parsed.netloc)
            or parsed.path not in ("", "/") or "?" in value or "#" in value
            or any(char.isspace() for char in value)):
        raise ValueError("campaign click base URL must be an HTTPS origin")
    # Accessing port also validates its range.
    port = parsed.port
    return "https://" + parsed.hostname + (":%d" % port if port is not None else "")


def is_preview(headers):
    return bool(PREVIEW_AGENT.search(headers.get("User-Agent", "")) or any(
        PREFETCH.search(headers.get(header, "")) for header in ("Sec-Purpose", "Purpose")
    ))


class ClickStore:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        root = env.get("OPENCLAW_STATE_DIR") or "/home/joseph/.openclaw"
        self.path = (Path(root) / "runtime" / "campaign-clicks.sqlite").absolute()

    def _connect(self, mode):
        return closing(sqlite3.connect(self.path.as_uri() + "?mode=" + mode,
                                        uri=True, timeout=0.5))

    def register(self, campaign, jid, name, destination):
        """Create or reuse a link before its caption is submitted to WhatsApp."""
        if not WHATSAPP_URL.fullmatch(destination):
            raise ValueError("invalid WhatsApp click-to-chat destination")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600))
        with self._connect("rw") as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            with db:
                db.execute("""
                    INSERT INTO click_groups VALUES (?, ?, ?)
                    ON CONFLICT (campaign, jid) DO UPDATE SET name = excluded.name
                    WHERE excluded.name != '' AND name != excluded.name
                    """, (campaign, jid, name or ""))
                db.execute("""
                    INSERT INTO click_links (id, campaign, jid, destination) VALUES (?, ?, ?, ?)
                    ON CONFLICT (campaign, jid, destination) DO NOTHING
                    """, (secrets.token_hex(16), campaign, jid, destination))
                return db.execute("""
                    SELECT id FROM click_links WHERE campaign = ? AND jid = ? AND destination = ?
                    """, (campaign, jid, destination)).fetchone()[0]

    def _created(self):
        try:
            self.path.stat()
        except FileNotFoundError:
            return False
        return True

    def destination(self, link_id):
        if not self._created():
            return None
        with self._connect("ro") as db:
            row = db.execute("SELECT destination FROM click_links WHERE id = ?", (link_id,)).fetchone()
        return row[0] if row and WHATSAPP_URL.fullmatch(row[0]) else None

    def increment(self, link_id):
        with self._connect("rw") as db, db:
            db.execute("UPDATE click_links SET clicks = clicks + 1 WHERE id = ?", (link_id,))

    def report(self, campaign):
        rows = []
        if self._created():
            with self._connect("ro") as db:
                rows = db.execute("""
                    SELECT g.jid, g.name, SUM(l.clicks) AS clicks
                    FROM click_groups AS g
                    JOIN click_links AS l USING (campaign, jid)
                    WHERE g.campaign = ?
                    GROUP BY g.jid, g.name
                    ORDER BY clicks DESC, g.name COLLATE NOCASE, g.jid
                    """, (campaign,)).fetchall()
        return {
            "totalClicks": sum(clicks for _, _, clicks in rows),
            "groups": [{"jid": jid, "name": name or jid, "clicks": clicks}
                       for jid, name, clicks in rows],
        }
