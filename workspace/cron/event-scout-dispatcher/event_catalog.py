"""Algeria Exhibitions catalog fetching, date-caching, and event matching.

Turns the scraped listing into this-month's datable events, caches resolved
dates under the runtime tree, and answers category/location availability
questions. Pure over the catalog data plus the check_new_events (ax) scraper;
no calendar writes or messaging (those live in the dispatcher).
"""

from __future__ import annotations

import calendar
from collections import Counter
from datetime import date
import json
import os
from pathlib import Path
import sys

WORKSPACE = Path(__file__).resolve().parents[2]
LISTENER_DIR = WORKSPACE / "cron" / "algeria-exhibitions-listener"
if str(LISTENER_DIR) not in sys.path:
    sys.path.insert(0, str(LISTENER_DIR))
import check_new_events as ax  # noqa: E402  (reuse fetch_list/fetch_dates/is_upcoming/fmt_range)

SOURCE_PREFIX = "algeria-exhibitions"

# Each run only syncs events that happen during the current calendar month.
# is_upcoming() already drops anything ending before today; this caps the far end
# at the last day of the month. Computed off ax.TODAY so both bounds share one "now".
MONTH_END = date(ax.TODAY.year, ax.TODAY.month,
                 calendar.monthrange(ax.TODAY.year, ax.TODAY.month)[1])

# Resolving an event's dates costs one (sometimes retried) HTTP request per event, so we
# cache resolved dates by event id under the writable runtime tree — same place the
# listener keeps its state. Each run then only fetches dates for events that are new,
# whose slug changed, or that still have no concrete date published.
DATE_CACHE_FILE = os.environ.get(
    "OPENCLAW_EVENT_SCOUT_DATE_CACHE",
    "/home/joseph/.openclaw/runtime/cron-state/event-scout-dispatcher/date_cache.json",
)


def happens_this_month(start: str, end: str) -> bool:
    """True if the event overlaps [today, MONTH_END].

    is_upcoming() already guarantees end >= today, so the only extra test is that
    the event starts on or before month-end (missing start falls back to end).
    """
    start_d = ax.parse_d(start) or ax.parse_d(end)
    return start_d is not None and start_d <= MONTH_END


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
