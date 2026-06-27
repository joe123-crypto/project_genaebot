#!/usr/bin/env python3
"""Per-user event-scout dispatcher.

Finds Algeria Exhibitions events for each registered user and, by default, DMs a
numbered offer list so the user can choose what goes into their calendar. A user
can explicitly opt into auto-add mode; only then does the scheduled run write all
matches directly through the Phase A calendar bridge.

Each run is scoped to the current calendar month: it only offers or syncs events
that happen between today and the last day of the month, so a single weekly run
never dumps far-future events onto a user.

Std-lib only. Reuses the Algeria Exhibitions fetch helpers from the listener cron
(which it never mutates) and shells out to event_subscriptions.py + genaie-calendar
so the registry-locking and bridge logic each live in exactly one place.

Mirrors the personalized-news model: one shared dispatcher, no per-user cron.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

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
DEFAULT_DELIVERY_MODE = "approval"
WP_API_BASE = "https://www.algeriaexhibitions.dz/en/wp-json/wp/v2"
TAXONOMY_PAGE_SIZES = (50, 10, None)
TAXONOMY_TIMEOUT_SECONDS = 4

# Each run only syncs events that happen during the current calendar month.
# is_upcoming() already drops anything ending before today; this caps the far end
# at the last day of the month. Computed off ax.TODAY so both bounds share one "now".
MONTH_END = date(ax.TODAY.year, ax.TODAY.month,
                 calendar.monthrange(ax.TODAY.year, ax.TODAY.month)[1])


def happens_this_month(start: str, end: str) -> bool:
    """True if the event overlaps [today, MONTH_END].

    is_upcoming() already guarantees end >= today, so the only extra test is that
    the event starts on or before month-end (missing start falls back to end).
    """
    start_d = ax.parse_d(start) or ax.parse_d(end)
    return start_d is not None and start_d <= MONTH_END

# Resolving an event's dates costs one (sometimes retried) HTTP request per event, so we
# cache resolved dates by event id under the writable runtime tree — same place the
# listener keeps its state. Each run then only fetches dates for events that are new,
# whose slug changed, or that still have no concrete date published.
DATE_CACHE_FILE = os.environ.get(
    "OPENCLAW_EVENT_SCOUT_DATE_CACHE",
    "/home/joseph/.openclaw/runtime/cron-state/event-scout-dispatcher/date_cache.json",
)


def load_date_cache() -> dict:
    try:
        with open(DATE_CACHE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_date_cache(cache: dict) -> None:
    # The cache is a pure optimization; a write failure must never break a dispatch run.
    try:
        os.makedirs(os.path.dirname(DATE_CACHE_FILE), exist_ok=True)
        tmp = f"{DATE_CACHE_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False)
        os.replace(tmp, DATE_CACHE_FILE)
    except OSError:
        pass


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


def catalog_options_from_listing(listing: dict) -> dict:
    categories = Counter()
    locations = Counter()
    for meta in listing.values():
        category = (meta.get("category") or "general").strip().lower()
        location = (meta.get("location") or "").strip()
        if category:
            categories[category] += 1
        if location:
            locations[location] += 1
    return {
        "categories": [
            {"name": name, "count": count}
            for name, count in sorted(categories.items(), key=lambda item: (-item[1], item[0]))
        ],
        "locations": [
            {"name": name, "count": count}
            for name, count in sorted(locations.items(), key=lambda item: (-item[1], item[0]))
        ],
    }


def normalize_taxonomy_slug(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("-", " ")).strip().lower()


def taxonomy_url(taxonomy: str, page_size: int | None, page: int) -> str:
    params = {"_fields": "id,name,slug,count", "page": str(page)}
    if page_size:
        params["per_page"] = str(page_size)
    return f"{WP_API_BASE}/{taxonomy}?{urlencode(params)}"


def header_int(headers, name: str, default: int) -> int:
    if headers is None:
        return default
    try:
        return int(headers.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def taxonomy_http_get(url: str) -> tuple[str | None, object, str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ax.UA})
        with urllib.request.urlopen(req, timeout=TAXONOMY_TIMEOUT_SECONDS) as resp:
            return resp.read().decode("utf-8", "ignore"), resp.headers, ""
    except urllib.error.HTTPError as err:
        return None, None, f"HTTP {err.code}: {err.reason}"
    except Exception as err:
        return None, None, f"{type(err).__name__}: {err}"


def fetch_taxonomy_page(taxonomy: str, page_size: int | None, page: int) -> tuple[list[dict], object]:
    body, headers, error = taxonomy_http_get(taxonomy_url(taxonomy, page_size, page))
    if body is None:
        label = f"per_page={page_size}" if page_size else "default page size"
        detail = f": {error}" if error else ""
        raise RuntimeError(f"could not fetch {taxonomy} terms with {label}, page {page}{detail}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as err:
        raise RuntimeError(f"invalid Algeria Exhibitions {taxonomy} term payload") from err
    if not isinstance(payload, list):
        raise RuntimeError(f"unexpected Algeria Exhibitions {taxonomy} term payload")
    return payload, headers


def fetch_taxonomy_terms(taxonomy: str) -> list[dict]:
    errors = []
    for page_size in TAXONOMY_PAGE_SIZES:
        try:
            first_page, headers = fetch_taxonomy_page(taxonomy, page_size, 1)
            total_pages = max(1, header_int(headers, "X-WP-TotalPages", 1))
            terms = list(first_page)
            for page in range(2, total_pages + 1):
                page_terms, _ = fetch_taxonomy_page(taxonomy, page_size, page)
                terms.extend(page_terms)
            return terms
        except RuntimeError as err:
            label = f"per_page={page_size}" if page_size else "default"
            errors.append(f"{label}: {err}")
    raise RuntimeError(f"could not verify Algeria Exhibitions {taxonomy} terms ({'; '.join(errors)})")


def catalog_options_from_taxonomy() -> dict:
    categories = []
    for term in fetch_taxonomy_terms("eventcat"):
        slug = normalize_taxonomy_slug(term.get("slug"))
        if not slug:
            continue
        categories.append({"name": slug, "count": int(term.get("count") or 0)})

    locations = []
    location_error = ""
    try:
        location_terms = fetch_taxonomy_terms("location")
    except RuntimeError as err:
        location_terms = []
        location_error = str(err)
    for term in location_terms:
        slug = normalize_taxonomy_slug(term.get("slug"))
        if not slug:
            continue
        name = "SAFEX, Algiers" if slug == "safex" else re.sub(r"\s+", " ", str(term.get("name") or slug)).strip()
        locations.append({"name": name, "count": int(term.get("count") or 0)})

    return {
        "categories": sorted(categories, key=lambda item: (-item["count"], item["name"])),
        "locations": sorted(locations, key=lambda item: (-item["count"], item["name"])),
        "source": "taxonomy",
        "location_error": location_error,
    }


def fetch_catalog_options() -> dict:
    return catalog_options_from_taxonomy()


def should_fetch_taxonomy_categories(subscribers: list[dict], available_categories: set[str]) -> bool:
    for sub in subscribers:
        categories = sub.get("events", {}).get("categories", [])
        if categories and unavailable_categories(categories, available_categories):
            return True
    return False


def fetch_catalog_listing() -> dict:
    listing = ax.fetch_list()
    if listing is None:
        raise RuntimeError("could not fetch Algeria Exhibitions event list")
    return listing


def build_upcoming_events(listing: dict) -> list[dict]:
    """Keep this month's datable events from an already-fetched catalog listing.

    "This month" means the event overlaps [today, last day of the current month]
    (see happens_this_month); far-future events are left for the run in their month.

    Resolved event dates are cached by id (see DATE_CACHE_FILE): a cached entry is reused
    only when the slug is unchanged and at least one concrete date is known, so newly
    published dates are still picked up while already-dated events skip the per-event
    HTTP fetch. The rebuilt cache is pruned to the current catalog before saving.
    """
    cache = load_date_cache()
    fresh: dict[str, dict] = {}
    events = []
    for eid, meta in listing.items():
        key = str(eid)
        slug = meta.get("slug", "")
        cached = cache.get(key)
        if cached and cached.get("slug") == slug and (cached.get("start") or cached.get("end")):
            start, end = cached.get("start"), cached.get("end")
        elif slug:
            start, end = ax.fetch_dates(slug)
        else:
            start, end = (None, None)
        fresh[key] = {"slug": slug, "start": start, "end": end}
        if not ax.is_upcoming(start, end):
            continue
        # A calendar entry needs at least one concrete date; without one we leave the
        # event unsynced so a later run can pick it up once the site publishes dates.
        start_date = start or end
        end_date = end or start
        if not start_date:
            continue
        if not happens_this_month(start_date, end_date):
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
    save_date_cache(fresh)
    return events


def fetch_upcoming_events() -> list[dict]:
    return build_upcoming_events(fetch_catalog_listing())


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


def unavailable_categories(categories: list[str], available_categories: set[str]) -> list[str]:
    missing = []
    for category in categories or []:
        if not any(category in available or available in category for available in available_categories):
            missing.append(category)
    return missing


def event_snapshot(event: dict) -> dict:
    return {
        "source_id": event.get("source_id", ""),
        "summary": event.get("summary", ""),
        "location": event.get("location", ""),
        "source_url": event.get("source_url", ""),
        "category": event.get("category", ""),
        "start_date": event.get("start_date", ""),
        "end_date": event.get("end_date", ""),
        "range": event.get("range", ""),
    }


def offer_events(phone: str, events: list[dict]) -> dict:
    payload = json.dumps([event_snapshot(event) for event in events], ensure_ascii=False)
    return subs_cmd("record-offers", "--phone", phone, "--events-json", payload)


def send_whatsapp(phone: str, message: str) -> bool:
    try:
        proc = subprocess.run(
            ["openclaw", "message", "send", "--channel", "whatsapp", "--target", phone, "--message", message],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return False
    return proc.returncode == 0


def send_added_dm(phone: str, added: int) -> None:
    """Best-effort one-line WhatsApp summary. Never fatal to the run."""
    plural = "s" if added != 1 else ""
    send_whatsapp(phone, f"Added {added} event{plural} to your Google Calendar with reminders.")


def send_offer_dm(phone: str, offers: list[dict]) -> None:
    if not offers:
        return
    lines = ["I found these matching events. Reply with the numbers you want in your calendar:"]
    for offer in offers:
        number = offer.get("choice_number", "?")
        date_range = offer.get("range") or offer.get("start_date", "")
        details = " | ".join(
            value for value in [date_range, offer.get("location", ""), offer.get("category", "")] if value
        )
        lines.append(f"{number}. {offer.get('summary', 'Event')} - {details}")
        if offer.get("source_url"):
            lines.append(offer["source_url"])
    send_whatsapp(phone, "\n".join(lines))


def process_auto_add(sub: dict, events: list[dict], dry_run: bool, send_dms: bool) -> dict:
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

    matches = [
        e for e in events
        if e["source_id"] not in synced and event_matches(e, categories, location)
    ]

    if dry_run:
        result.update(status="dry-run", would_write=[e["source_id"] for e in matches])
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
    for event in matches:
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
        send_added_dm(phone, added)
    return result


def category_check_unavailable_result(sub: dict, categories: list[str], reason: str) -> dict:
    return {
        "phone_hash": sub.get("phone_hash"),
        "status": "category-check-unavailable",
        "requested_categories": categories,
        "reason": reason[:240],
    }


def process_approval(
    sub: dict,
    events: list[dict],
    available_categories: set[str],
    dry_run: bool,
    send_dms: bool,
    categories_verified: bool = True,
    category_verification_error: str = "",
) -> dict:
    phone = sub.get("phone")
    info = sub.get("events", {})
    user_id = info.get("user_id")
    result = {"phone_hash": sub.get("phone_hash")}

    if not user_id:
        result.update(status="skipped", reason="no user_id")
        return result

    categories = info.get("categories", [])
    location = info.get("location", "")
    missing_categories = unavailable_categories(categories, available_categories)
    if missing_categories:
        if not categories_verified:
            return category_check_unavailable_result(
                sub,
                categories,
                category_verification_error or "available categories could not be verified",
            )
        result.update(
            status="needs-category-choice",
            unavailable_categories=missing_categories,
            available_categories=sorted(available_categories),
        )
        return result

    synced = set(info.get("synced_event_ids", []))
    offered = set(info.get("offered_event_ids", []))
    pending_ids = {
        offer.get("source_id")
        for offer in info.get("pending_offers", [])
        if offer.get("source_id")
    }
    already_handled = synced | offered | pending_ids
    matches = [
        e for e in events
        if e["source_id"] not in already_handled and event_matches(e, categories, location)
    ]

    if dry_run:
        result.update(status="dry-run", would_offer=[e["source_id"] for e in matches])
        return result

    if not matches:
        result.update(status="no_matches", offered=0)
        return result

    recorded = offer_events(phone, matches)
    added_offers = recorded.get("added_offers", [])
    result.update(
        status="offered",
        offered=len(added_offers),
        added_offers=added_offers,
        pending_total=len(recorded.get("events", {}).get("pending_offers", [])),
    )
    if send_dms and added_offers:
        send_offer_dm(phone, added_offers)
    return result


def process_subscriber(
    sub: dict,
    events: list[dict],
    available_categories: set[str],
    dry_run: bool,
    send_dms: bool,
    categories_verified: bool = True,
    category_verification_error: str = "",
) -> dict:
    info = sub.get("events", {})
    missing_categories = unavailable_categories(info.get("categories", []), available_categories)
    if missing_categories:
        if not categories_verified:
            return category_check_unavailable_result(
                sub,
                info.get("categories", []),
                category_verification_error or "available categories could not be verified",
            )
        return {
            "phone_hash": sub.get("phone_hash"),
            "status": "needs-category-choice",
            "unavailable_categories": missing_categories,
            "available_categories": sorted(available_categories),
        }
    mode = sub.get("events", {}).get("delivery_mode") or DEFAULT_DELIVERY_MODE
    if mode == "auto_add":
        return process_auto_add(sub, events, dry_run=dry_run, send_dms=send_dms)
    return process_approval(
        sub,
        events,
        available_categories,
        dry_run=dry_run,
        send_dms=send_dms,
        categories_verified=categories_verified,
        category_verification_error=category_verification_error,
    )


def parse_selection_values(values: list[str]) -> tuple[list[int], list[str]]:
    indexes = []
    source_ids = []
    for value in values or []:
        for token in re.findall(rf"{SOURCE_PREFIX}:\d+|\d+", str(value)):
            if token.isdigit():
                indexes.append(int(token))
            else:
                source_ids.append(token)
    return indexes, source_ids


def choose_pending_offers(pending_offers: list[dict], selections: list[str]) -> tuple[list[dict], list[str]]:
    indexes, source_ids = parse_selection_values(selections)
    selected = []
    invalid = []
    seen = set()
    by_source_id = {offer.get("source_id"): offer for offer in pending_offers}
    for index in indexes:
        if index < 1 or index > len(pending_offers):
            invalid.append(str(index))
            continue
        offer = pending_offers[index - 1]
        source_id = offer.get("source_id")
        if source_id not in seen:
            selected.append(offer)
            seen.add(source_id)
    for source_id in source_ids:
        offer = by_source_id.get(source_id)
        if not offer:
            invalid.append(source_id)
            continue
        if source_id not in seen:
            selected.append(offer)
            seen.add(source_id)
    return selected, invalid


def save_selection(args: argparse.Namespace) -> dict:
    if not args.phone:
        raise RuntimeError("--phone is required with --select")
    sub = subs_cmd("get", "--phone", args.phone)
    info = sub.get("events", {})
    pending_offers = info.get("pending_offers", [])
    selected, invalid = choose_pending_offers(pending_offers, args.select)
    if invalid:
        return {
            "status": "invalid_selection",
            "invalid": invalid,
            "pending": len(pending_offers),
        }
    if not selected:
        return {
            "status": "needs_selection",
            "pending": len(pending_offers),
        }
    if args.dry_run:
        return {
            "status": "dry-run",
            "would_write": [offer.get("source_id") for offer in selected],
        }

    user_id = info.get("user_id")
    if not user_id:
        return {"status": "skipped", "reason": "no user_id"}
    status = calendar_status(user_id)
    if not status.get("calendarConnected"):
        subs_cmd("record-delivery", "--phone", args.phone, "--status", "skipped", "--message", "calendar not connected")
        return {"status": "skipped", "reason": "calendar not connected"}

    reminder_minutes = info.get("reminder_minutes") or list(DEFAULT_REMINDER_MINUTES)
    added = []
    failed = []
    for event in selected:
        try:
            resp = write_event(user_id, event, reminder_minutes)
        except (RuntimeError, subprocess.SubprocessError) as err:
            failed.append({"source_id": event.get("source_id"), "error": str(err)[:240]})
            continue
        if resp.get("status") in ("created", "exists"):
            subs_cmd("mark-synced", "--phone", args.phone, "--event-id", event["source_id"])
            subs_cmd("clear-pending", "--phone", args.phone, "--event-id", event["source_id"])
            added.append({"source_id": event.get("source_id"), "summary": event.get("summary")})
        else:
            failed.append({"source_id": event.get("source_id"), "error": str(resp)[:240]})

    delivery_status = "failed" if failed and not added else "saved"
    subs_cmd("record-delivery", "--phone", args.phone, "--status", delivery_status,
             "--message", f"saved {len(added)}, failed {len(failed)}")
    return {
        "status": delivery_status,
        "added": added,
        "failed": failed,
    }


def dispatch(args: argparse.Namespace) -> dict:
    listing = fetch_catalog_listing()
    options = catalog_options_from_listing(listing)
    available_categories = {item["name"] for item in options["categories"]}
    category_verification_error = ""
    categories_verified = False
    events = build_upcoming_events(listing)
    if args.phone:
        sub = subs_cmd("get", "--phone", args.phone)
        subscribers = [sub] if sub.get("events", {}).get("enabled") else []
        skipped_not_allowlisted = 0
    else:
        subs = subs_cmd("list-enabled", "--allowlisted-only")
        subscribers = subs.get("subscribers", [])
        skipped_not_allowlisted = subs.get("skipped_not_allowlisted", 0)
    if should_fetch_taxonomy_categories(subscribers, available_categories):
        try:
            taxonomy_options = fetch_catalog_options()
        except RuntimeError as err:
            category_verification_error = str(err)
        else:
            available_categories.update(item["name"] for item in taxonomy_options["categories"])
            options["available_category_source"] = taxonomy_options.get("source", "taxonomy")
            categories_verified = True

    results = [
        process_subscriber(
            sub,
            events,
            available_categories,
            dry_run=args.dry_run,
            send_dms=not args.no_dm,
            categories_verified=categories_verified,
            category_verification_error=category_verification_error,
        )
        for sub in subscribers
    ]
    return {
        "dry_run": bool(args.dry_run),
        "subscribers": len(subscribers),
        "skipped_not_allowlisted": skipped_not_allowlisted,
        "events_this_month": len(events),
        "available_categories": options["categories"],
        "available_locations": options["locations"],
        "category_check_verified": categories_verified,
        "category_verification_error": category_verification_error,
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be offered/written; no writes or mutations")
    parser.add_argument("--no-dm", action="store_true", help="do not send WhatsApp DMs")
    parser.add_argument("--phone", default="", help="run only for this WhatsApp phone")
    parser.add_argument("--list-categories", action="store_true",
                        help="print current Algeria Exhibitions categories/locations and exit")
    parser.add_argument("--select", action="append", default=[],
                        help="pending offer number or source id to save; repeat or pass a comma-separated list")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.list_categories:
            summary = fetch_catalog_options()
        elif args.select:
            summary = save_selection(args)
        else:
            summary = dispatch(args)
    except Exception as err:
        print(json.dumps({"status": "error", "message": str(err)}), file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", **summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
