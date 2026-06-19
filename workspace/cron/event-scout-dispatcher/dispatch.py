#!/usr/bin/env python3
"""Per-user event-scout dispatcher.

Syncs upcoming Algeria Exhibitions events into each registered user's own Google
Calendar through the Phase A calendar bridge, with per-user reminders.

Std-lib only. Reuses the Algeria Exhibitions fetch helpers from the listener cron
(which it never mutates) and shells out to event_subscriptions.py + genaie-calendar
so the registry-locking and bridge logic each live in exactly one place.

Mirrors the personalized-news model: one shared dispatcher, no per-user cron.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# bin/, lib/, public-events/, and cron/ are siblings under workspace/, so resolve
# everything relative to this file (works in the deployed tree and in a worktree).
WORKSPACE = Path(__file__).resolve().parents[2]
LISTENER_DIR = WORKSPACE / "cron" / "algeria-exhibitions-listener"
EVENT_SUBS = WORKSPACE / "public-events" / "event_subscriptions.py"
GENAIE_CALENDAR = WORKSPACE / "bin" / "genaie-calendar"

if str(LISTENER_DIR) not in sys.path:
    sys.path.insert(0, str(LISTENER_DIR))
import check_new_events as ax  # noqa: E402  (reuse fetch_list/fetch_dates/is_upcoming/fmt_range)


SOURCE_PREFIX = "algeria-exhibitions"
DEFAULT_REMINDER_MINUTES = [1440, 120]


def run_json(cmd: list[str], timeout: int = 90) -> dict:
    """Run a helper expected to print one JSON object on stdout; return it parsed."""
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        raise RuntimeError(err or out or f"command failed: {cmd[1] if len(cmd) > 1 else cmd[0]}")
    try:
        return json.loads(out) if out else {}
    except json.JSONDecodeError as err:
        raise RuntimeError(f"non-JSON output from {Path(cmd[1]).name if len(cmd) > 1 else cmd[0]}: {out[:200]}") from err


def subs_cmd(*args: str) -> dict:
    return run_json([sys.executable, str(EVENT_SUBS), *args])


def calendar_status(user_id: str) -> dict:
    return run_json([sys.executable, str(GENAIE_CALENDAR), "status", "--sender-uid", user_id])


def write_event(user_id: str, event: dict, reminder_minutes: list[int]) -> dict:
    cmd = [
        sys.executable, str(GENAIE_CALENDAR), "write",
        "--sender-uid", user_id,
        "--source-id", event["source_id"],
        "--summary", event["summary"],
        "--start-date", event["start_date"],
        "--end-date", event["end_date"],
        "--force",
    ]
    if event.get("location"):
        cmd += ["--location", event["location"]]
    if event.get("source_url"):
        cmd += ["--source-url", event["source_url"]]
    for minutes in reminder_minutes:
        cmd += ["--reminder-min", str(minutes)]
    return run_json(cmd)


def fetch_upcoming_events() -> list[dict]:
    """Fetch the Algeria Exhibitions catalog once and keep upcoming, datable events."""
    listing = ax.fetch_list()
    if listing is None:
        raise RuntimeError("could not fetch Algeria Exhibitions event list")
    events = []
    for eid, meta in listing.items():
        slug = meta.get("slug", "")
        start, end = ax.fetch_dates(slug) if slug else (None, None)
        if not ax.is_upcoming(start, end):
            continue
        # A calendar entry needs at least one concrete date; without one we leave the
        # event unsynced so a later run can pick it up once the site publishes dates.
        start_date = start or end
        end_date = end or start
        if not start_date:
            continue
        events.append(
            {
                "source_id": f"{SOURCE_PREFIX}:{eid}",
                "summary": (meta.get("title") or "").strip() or f"Event {eid}",
                "location": meta.get("location", ""),
                "source_url": meta.get("link", ""),
                "category": (meta.get("category", "") or "").lower(),
                "start_date": start_date,
                "end_date": end_date,
                "range": ax.fmt_range(start, end),
            }
        )
    return events


def event_matches(event: dict, categories: list[str], location: str) -> bool:
    if categories:
        cat = event.get("category", "")
        if not any(c in cat or cat in c for c in categories):
            return False
    if location:
        loc = (event.get("location", "") or "").lower()
        if location.lower() not in loc:
            return False
    return True


def send_dm(phone: str, added: int) -> None:
    """Best-effort one-line WhatsApp summary. Never fatal to the run."""
    plural = "s" if added != 1 else ""
    message = f"Added {added} event{plural} to your Google Calendar with reminders."
    try:
        subprocess.run(
            ["openclaw", "message", "send", "--channel", "whatsapp", "--target", phone, "--message", message],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        pass


def process_subscriber(sub: dict, events: list[dict], dry_run: bool, send_dms: bool) -> dict:
    phone = sub.get("phone")
    info = sub.get("events", {})
    user_id = info.get("user_id")
    result = {"phone_hash": sub.get("phone_hash")}

    if not user_id:
        result.update(status="skipped", reason="no user_id")
        return result

    categories = info.get("categories", [])
    location = info.get("location", "")
    reminder_minutes = info.get("reminder_minutes") or list(DEFAULT_REMINDER_MINUTES)
    synced = set(info.get("synced_event_ids", []))

    pending = [
        e for e in events
        if e["source_id"] not in synced and event_matches(e, categories, location)
    ]

    if dry_run:
        result.update(status="dry-run", would_write=[e["source_id"] for e in pending])
        return result

    # Skip (do not error) users who have not connected calendar access.
    try:
        status = calendar_status(user_id)
    except (RuntimeError, subprocess.SubprocessError) as err:
        subs_cmd("record-delivery", "--phone", phone, "--status", "skipped", "--message", str(err)[:240])
        result.update(status="skipped", reason=str(err)[:240])
        return result
    if not status.get("calendarConnected"):
        subs_cmd("record-delivery", "--phone", phone, "--status", "skipped", "--message", "calendar not connected")
        result.update(status="skipped", reason="calendar not connected")
        return result

    added = 0
    failed = 0
    for event in pending:
        try:
            resp = write_event(user_id, event, reminder_minutes)
        except (RuntimeError, subprocess.SubprocessError):
            failed += 1
            continue
        if resp.get("status") in ("created", "exists"):
            # Commit only after a confirmed response; idempotency is guaranteed by the
            # (senderUid, sourceId) key plus this local synced_event_ids short-circuit.
            subs_cmd("mark-synced", "--phone", phone, "--event-id", event["source_id"])
            added += 1
        else:
            failed += 1

    delivery_status = "failed" if failed and not added else "delivered"
    subs_cmd("record-delivery", "--phone", phone, "--status", delivery_status,
             "--message", f"added {added}, failed {failed}")
    result.update(status=delivery_status, added=added, failed=failed)

    if send_dms and added:
        send_dm(phone, added)
    return result


def dispatch(args: argparse.Namespace) -> dict:
    subs = subs_cmd("list-enabled", "--allowlisted-only")
    subscribers = subs.get("subscribers", [])
    events = fetch_upcoming_events()

    results = [
        process_subscriber(sub, events, dry_run=args.dry_run, send_dms=not args.no_dm)
        for sub in subscribers
    ]
    return {
        "dry_run": bool(args.dry_run),
        "subscribers": len(subscribers),
        "events_upcoming": len(events),
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be written per subscriber; no writes or mutations")
    parser.add_argument("--no-dm", action="store_true", help="do not send the WhatsApp DM summary")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        summary = dispatch(args)
    except Exception as err:
        print(json.dumps({"status": "error", "message": str(err)}), file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", **summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
