#!/usr/bin/env python3
"""
Algeria Exhibitions new-event listener.

Polls the algeriaexhibitions.dz WordPress REST API, diffs against saved state,
and announces UPCOMING events to a WhatsApp group as they newly appear in the
registry. There is no webhook on the site, so this is a poll + diff (same pattern
as the cron/github-issue-tracker job).

This script OWNS delivery: it sends the WhatsApp message itself via
`openclaw message send` and advances its queue only after a confirmed send, so
delivery and progress are transactional. The cron agent is just a thin trigger
that runs this script.

Backlog drain: on the first run the existing upcoming events are loaded into
announce_queue and released BATCH_SIZE (10) per run. A spacing gate
(MIN_INTERVAL_DAYS) ensures at most one batch goes out per ~week, so people have
time to digest before the next batch.

Usage:
  check_new_events.py            normal run (enrich, gate, send a batch, commit)
  check_new_events.py --dry-run  show the batch that WOULD be sent; no send, no
                                  state mutation

Std-lib only.
"""

import json
import os
import re
import sys
import time
import html
import subprocess
import urllib.request
from datetime import datetime, date, timedelta

API_BASE = "https://www.algeriaexhibitions.dz/en/wp-json/wp/v2/event"
EVENT_PAGE = "https://www.algeriaexhibitions.dz/en/event/{slug}/"
STATE_FILE = os.environ.get(
    "OPENCLAW_ALGERIA_EXHIBITIONS_STATE",
    "/home/joseph/.openclaw/runtime/cron-state/algeria-exhibitions-listener/state.json",
)
GROUP_TARGET = "120363429214832655@g.us"
BATCH_SIZE = 10
MIN_INTERVAL_DAYS = 6
UA = "Mozilla/5.0 (AlgeriaExhibitionsListener/1.0)"
TODAY = date.today()
MON = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
       "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---------------------------------------------------------------- fetching ----
def http_get(url, want_headers=False, timeout=25):
    """GET with one retry. Returns (body_str, headers) or (None, None) on failure."""
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "ignore")
                return (body, r.headers) if want_headers else (body, None)
        except Exception:
            if attempt == 0:
                time.sleep(2)
                continue
            return (None, None)


def strip_tags(s):
    return re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", html.unescape(s or ""))).strip()


def slug_value(class_list, prefix):
    for s in class_list or []:
        if s.startswith(prefix):
            return s[len(prefix):]
    return None


def fetch_list():
    """Return {id: {slug, title, link, category, location}} across all pages, or None."""
    current = {}
    body, headers = http_get(f"{API_BASE}?per_page=50&page=1", want_headers=True)
    if body is None:
        sys.stderr.write("ERROR: could not fetch event list page 1\n")
        return None
    total_pages = 1
    if headers is not None:
        try:
            total_pages = int(headers.get("X-WP-TotalPages", "1"))
        except (TypeError, ValueError):
            total_pages = 1

    def ingest(payload):
        for e in payload:
            cl = e.get("class_list", [])
            cat = slug_value(cl, "eventcat-")
            loc = slug_value(cl, "location-")
            current[e["id"]] = {
                "slug": e.get("slug", ""),
                "title": strip_tags(e.get("title", {}).get("rendered", "")),
                "link": e.get("link", ""),
                "category": (cat or "general").replace("-", " "),
                "location": "SAFEX, Algiers" if (loc in (None, "safex"))
                            else loc.replace("-", " ").title(),
            }

    try:
        ingest(json.loads(body))
    except json.JSONDecodeError:
        sys.stderr.write("ERROR: page 1 was not valid JSON\n")
        return None
    for page in range(2, total_pages + 1):
        b, _ = http_get(f"{API_BASE}?per_page=50&page={page}")
        if b is None:
            continue
        try:
            ingest(json.loads(b))
        except json.JSONDecodeError:
            continue
    return current


def fetch_dates(slug):
    """Return (start, end) 'YYYY-MM-DD' from the page schema.org JSON-LD, or (None, None)."""
    body, _ = http_get(EVENT_PAGE.format(slug=slug))
    if not body:
        return (None, None)
    ms = re.search(r'"startDate":\s*"([^"]+)"', body)
    me = re.search(r'"endDate":\s*"([^"]+)"', body)
    return (ms.group(1)[:10] if ms else None,
            me.group(1)[:10] if me else None)


# ------------------------------------------------------------------- dates ----
def parse_d(s):
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def is_upcoming(start, end):
    """Upcoming if end (or start fallback) is today or later. No date -> keep (can't prove past)."""
    ref = parse_d(end) or parse_d(start)
    return True if ref is None else ref >= TODAY


def sort_key(ev):
    return (parse_d(ev.get("start")) or date(9999, 12, 31), ev.get("title", ""))


def fmt_range(start, end):
    sd, ed = parse_d(start), parse_d(end)
    if sd is None and ed is None:
        return "dates TBC — see link"
    if sd and ed:
        if (sd.year, sd.month) == (ed.year, ed.month):
            return f"{sd.day}–{ed.day} {MON[ed.month]} {ed.year}"
        if sd.year == ed.year:
            return f"{sd.day} {MON[sd.month]} – {ed.day} {MON[ed.month]} {ed.year}"
        return f"{sd.day} {MON[sd.month]} {sd.year} – {ed.day} {MON[ed.month]} {ed.year}"
    d = sd or ed
    return f"{d.day} {MON[d.month]} {d.year}"


def format_message(batch, remaining_after):
    lines = ["\U0001F195 *Algeria Exhibitions — upcoming events*", ""]
    for ev in batch:
        lines.append(
            f"• *{ev['title']}* — {fmt_range(ev['start'], ev['end'])} "
            f"· {ev['location']} · {ev['category']}"
        )
        if ev["link"]:
            lines.append(f"  {ev['link']}")
    if remaining_after:
        lines.append("")
        lines.append(f"_Showing {len(batch)} · {remaining_after} more over the coming weeks._")
    return "\n".join(lines)


# ------------------------------------------------------------------- state ----
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"seen_ids": [], "announce_queue": [], "pending": [],
                "batch_size": BATCH_SIZE, "min_interval_days": MIN_INTERVAL_DAYS,
                "last_sent_at": None}
    with open(STATE_FILE) as f:
        st = json.load(f)
    st.setdefault("seen_ids", [])
    st.setdefault("announce_queue", [])
    st.setdefault("pending", [])
    st.setdefault("batch_size", BATCH_SIZE)
    st.setdefault("min_interval_days", MIN_INTERVAL_DAYS)
    st.setdefault("last_sent_at", None)
    return st


def save_state(st, total):
    st["last_total"] = total
    st["checked_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, STATE_FILE)


def within_interval(last_sent_at, days):
    if not last_sent_at:
        return False
    try:
        last = datetime.fromisoformat(last_sent_at)
    except (TypeError, ValueError):
        return False
    return (datetime.now(last.tzinfo) - last) < timedelta(days=days)


# -------------------------------------------------------------------- send ----
def send_message(text):
    """Send via openclaw. Returns (ok, combined_output)."""
    cmd = ["openclaw", "message", "send", "--channel", "whatsapp",
           "--target", GROUP_TARGET, "--message", text, "--json"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as ex:
        return (False, f"exec error: {ex}")
    out = (p.stdout or "") + (p.stderr or "")
    ok = (p.returncode == 0) and ('"messageId"' in out) and ('"error"' not in p.stdout)
    return (ok, out)


# -------------------------------------------------------------------- main ----
def main():
    dry = "--dry-run" in sys.argv[1:]

    current = fetch_list()
    if current is None:
        print("NO_NEW_EVENTS")          # total registry failure: stay silent
        return 0

    st = load_state()
    seen = set(st["seen_ids"])
    queue = st["announce_queue"]
    pending = list(st.get("pending", []))
    queued_ids = {ev["id"] for ev in queue}

    # Enrich genuinely-new ids (first run: everything).
    for eid in [e for e in current if e not in seen]:
        info = current[eid]
        start, end = fetch_dates(info["slug"])
        if is_upcoming(start, end) and eid not in queued_ids:
            queue.append({"id": eid, "title": info["title"], "link": info["link"],
                          "category": info["category"], "location": info["location"],
                          "start": start, "end": end})
            queued_ids.add(eid)

    # seen is a fetch-dedup cache; the queue is the transactional unit.
    seen.update(current.keys())
    queue = [ev for ev in queue if is_upcoming(ev.get("start"), ev.get("end"))]
    queue.sort(key=sort_key)

    # Select the batch (pending retry takes priority and is exempt from the gate).
    pending_set = set(pending)
    batch = [ev for ev in queue if ev["id"] in pending_set] if pending else []
    if pending and not batch:
        pending = []          # pending events lapsed/vanished; fall through

    gated = within_interval(st.get("last_sent_at"), st["min_interval_days"])
    if not pending:
        if gated and not dry:
            st["seen_ids"] = sorted(seen)
            st["announce_queue"] = queue
            st["pending"] = []
            save_state(st, len(current))
            print("SKIP: within spacing window (last sent %s)" % st.get("last_sent_at"))
            return 0
        batch = queue[:st["batch_size"]]

    if not batch:
        st["seen_ids"] = sorted(seen)
        st["announce_queue"] = queue
        st["pending"] = []
        if not dry:
            save_state(st, len(current))
        print("NO_NEW_EVENTS")
        return 0

    batch_ids = [ev["id"] for ev in batch]
    batch_id_set = set(batch_ids)
    remaining_after = [ev for ev in queue if ev["id"] not in batch_id_set]
    text = format_message(batch, len(remaining_after))

    if dry:
        print(text)
        print("\n[dry-run] batch=%d remaining_after=%d gated=%s pending_retry=%s"
              % (len(batch), len(remaining_after), gated, bool(pending)))
        return 0

    # Persist the in-flight marker BEFORE sending: a crash leaves a retry marker.
    st["seen_ids"] = sorted(seen)
    st["announce_queue"] = queue
    st["pending"] = batch_ids
    save_state(st, len(current))

    ok, out = send_message(text)
    if not ok:
        sys.stderr.write("SEND FAILED (queue/pending preserved for retry):\n" + out + "\n")
        return 1

    # Commit: only path that advances progress.
    st["announce_queue"] = remaining_after
    st["pending"] = []
    st["last_sent_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    save_state(st, len(current))
    print("SENT %d (remaining %d)" % (len(batch), len(remaining_after)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
