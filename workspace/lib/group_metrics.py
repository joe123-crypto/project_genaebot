"""Read-only group observations. Never imported by targeting or cap planning."""
from contextlib import closing
import datetime
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import time

import group_discovery

WINDOW_MS = 24 * 3600 * 1000
MAX_HEARTBEAT_GAP_MS = 90 * 1000  # whatsapp-group-activity.mjs


def iso(stamp):
    return datetime.datetime.fromtimestamp(stamp / 1000, datetime.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def member_observations(root, now):
    latest = {}
    try:
        files = list((root / "runtime/whatsapp-groups").glob("*.json"))
    except OSError:
        return latest
    for filename in files:
        account = filename.stem
        if not group_discovery.ACCOUNT.fullmatch(account) or account in {"__proto__", "constructor", "prototype"}:
            continue
        try:
            inventory = group_discovery.read_inventory(filename, account)
        except (OSError, ValueError, TypeError, OverflowError):
            continue
        for jid, entry in inventory["groups"].items():
            count = entry.get("memberCount")
            try:
                observed = group_discovery.timestamp(entry.get("memberCountObservedAt")) * 1000
            except (ValueError, TypeError, OverflowError):
                continue
            if type(count) is not int or count < 0 or count > 2**53 - 1 or observed > now:
                continue
            if jid not in latest or observed > latest[jid][1]:
                latest[jid] = (count, observed)
    return latest


def bot_hashes(membership):
    result = []
    for account in ((membership.get("defaults") or {}).get("accounts") or {}).values():
        number = account.get("number") if isinstance(account, dict) else None
        if isinstance(number, str):
            digits = re.sub(r"\D", "", number)
            if digits:
                result.append(hashlib.sha256((digits + "@s.whatsapp.net").encode()).hexdigest())
    return result


def activity_observations(root, membership, now, live_enabled=True):
    """One SQLite read snapshot; missing/corrupt telemetry never fails a roster."""
    filename = root / "runtime/whatsapp-activity.sqlite"
    start = now - WINDOW_MS
    try:
        with closing(sqlite3.connect(filename.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)) as db:
            db.execute("PRAGMA query_only = ON")
            db.execute("BEGIN")
            hashes = bot_hashes(membership)
            exclusions, args = "", [start, now]
            if hashes:
                placeholders = ",".join("?" for _ in hashes)
                exclusions = (f" AND (sender_hash IS NULL OR sender_hash NOT IN ({placeholders}))"
                              f" AND (sender_alt_hash IS NULL OR sender_alt_hash NOT IN ({placeholders}))")
                args += hashes + hashes
            counts = dict(db.execute("SELECT jid, COUNT(*) FROM events WHERE message_at > ? AND message_at <= ?"
                                     + exclusions + " GROUP BY jid", args))
            intervals = {}
            for jid, first, last, live in db.execute(
                    "SELECT jid, started_at, through_at, live FROM coverage WHERE through_at >= ? ORDER BY started_at", (start,)):
                if not (isinstance(first, int) and isinstance(last, int) and first <= last <= now):
                    continue
                end = now if live_enabled and live == 1 and now - last <= MAX_HEARTBEAT_GAP_MS else last
                intervals.setdefault(jid, []).append((first, end))
            return counts, intervals
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return {}, {}


def attach(payload, membership, env=None, now=None):
    """Enrich only Groups API rows, keeping both console adapters identical."""
    env = os.environ if env is None else env
    now = int(time.time() * 1000) if now is None else int(now)
    root = Path(env.get("OPENCLAW_STATE_DIR") or "/home/joseph/.openclaw")
    members = member_observations(root, now)
    counts, intervals = activity_observations(root, membership, now, env.get("GENAIE_GROUP_ACTIVITY") != "0")
    start = now - WINDOW_MS
    for row in payload.get("groups", []) + payload.get("discoveredGroups", []):
        jid = row["jid"]
        observed = intervals.get(jid, [])
        covered_through = start
        for first, last in observed:
            if first > covered_through:
                break
            covered_through = max(covered_through, last)
        coverage = "complete" if covered_through >= now else "partial" if observed or jid in counts else "unavailable"
        member = members.get(jid)
        row["metrics"] = {
            "dailyMessageCount": counts.get(jid, 0) if coverage != "unavailable" else None,
            "windowStart": iso(start), "asOf": iso(now), "coverage": coverage,
            "memberCount": member[0] if member else None,
            "memberCountObservedAt": iso(member[1]) if member else None,
        }
    return payload
