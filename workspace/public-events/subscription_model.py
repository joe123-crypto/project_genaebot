"""Public-events subscription record model: normalization and shaping.

Category/reminder/delivery-mode/offer normalizers plus the subscriber record
builder and sanitizer. Pure data shaping over registry records (only phone-hash
and timestamp helpers touch shared lib code); the CLI handlers in
event_subscriptions.py call these.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys

# Shared registry helpers live in workspace/lib; resolve it relative to this
# script so it works in the deployed workspace and in a git worktree.
LIB_DIR = str(Path(__file__).resolve().parent.parent / "lib")
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)
from phone_utils import phone_hash  # noqa: E402
from registry_store import now_iso  # noqa: E402


# Stored as minutes-before-start to match the Phase A calendar API reminderOverrides.
DEFAULT_REMINDER_MINUTES = [1440, 120]
DEFAULT_DELIVERY_MODE = "approval"
DELIVERY_MODES = {"approval", "auto_add"}
OFFER_FIELDS = (
    "source_id",
    "summary",
    "location",
    "source_url",
    "category",
    "start_date",
    "end_date",
    "range",
)


def normalize_categories(values: list[str]) -> list[str]:
    """Lower-cased, whitespace-collapsed, de-duplicated event category filters."""
    deduped = []
    seen = set()
    for value in values or []:
        category = re.sub(r"\s+", " ", str(value or "")).strip(" .:-").lower()
        if category and category not in seen:
            seen.add(category)
            deduped.append(category)
    return deduped[:12]


def normalize_reminders(values: list[int]) -> list[int]:
    """De-duplicated, non-negative minutes-before-start, order preserved."""
    deduped = []
    seen = set()
    for value in values or []:
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"reminder minutes must be integers: {value!r}")
        if minutes < 0:
            raise ValueError("reminder minutes must be >= 0")
        if minutes not in seen:
            seen.add(minutes)
            deduped.append(minutes)
    return deduped


def normalize_delivery_mode(value: str | None) -> str:
    value = re.sub(r"[\s-]+", "_", str(value or DEFAULT_DELIVERY_MODE).strip().lower())
    aliases = {
        "manual": "approval",
        "pick": "approval",
        "approval": "approval",
        "auto": "auto_add",
        "auto_add": "auto_add",
        "autoadd": "auto_add",
    }
    mode = aliases.get(value, value)
    if mode not in DELIVERY_MODES:
        raise ValueError(f"delivery_mode must be one of: {', '.join(sorted(DELIVERY_MODES))}")
    return mode


def normalize_offer(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("event offer must be an object")
    offer = {field: re.sub(r"\s+", " ", str(raw.get(field, "") or "")).strip() for field in OFFER_FIELDS}
    if not offer["source_id"]:
        raise ValueError("event offer source_id is required")
    if not offer["summary"]:
        raise ValueError("event offer summary is required")
    if not offer["start_date"] or not offer["end_date"]:
        raise ValueError("event offer dates are required")
    if not offer["range"]:
        offer["range"] = offer["start_date"] if offer["start_date"] == offer["end_date"] else f"{offer['start_date']} to {offer['end_date']}"
    return offer


def normalize_pending_offers(values: list[dict]) -> list[dict]:
    offers = []
    seen = set()
    for raw in values or []:
        try:
            offer = normalize_offer(raw)
        except ValueError:
            continue
        if offer["source_id"] in seen:
            continue
        seen.add(offer["source_id"])
        offers.append(offer)
    return offers


def with_choice_numbers(offers: list[dict]) -> list[dict]:
    return [{**offer, "choice_number": index} for index, offer in enumerate(offers, start=1)]


def parse_offers_json(value: str) -> list[dict]:
    try:
        payload = json.loads(value or "[]")
    except json.JSONDecodeError as err:
        raise ValueError("events_json must be valid JSON") from err
    if not isinstance(payload, list):
        raise ValueError("events_json must be a JSON list")
    return [normalize_offer(item) for item in payload]


def subscriber_record(phone: str, existing: dict | None = None) -> dict:
    record = dict(existing or {})
    record["phone"] = phone
    record["phone_hash"] = phone_hash(phone)
    record.setdefault("created_at", now_iso())
    record["updated_at"] = now_iso()
    record.setdefault("services", {})
    return record


def sanitize_record(record: dict) -> dict:
    events = dict(record.get("services", {}).get("events", {}))
    try:
        delivery_mode = normalize_delivery_mode(events.get("delivery_mode"))
    except ValueError:
        delivery_mode = DEFAULT_DELIVERY_MODE
    pending_offers = normalize_pending_offers(events.get("pending_offers", []))
    return {
        "phone": record.get("phone"),
        "phone_hash": record.get("phone_hash"),
        "events": {
            "enabled": bool(events.get("enabled")),
            "user_id": events.get("user_id"),
            "categories": list(events.get("categories", [])),
            "location": events.get("location", ""),
            "reminder_minutes": list(events.get("reminder_minutes", []) or DEFAULT_REMINDER_MINUTES),
            "delivery_mode": delivery_mode,
            "synced_event_ids": list(events.get("synced_event_ids", [])),
            "offered_event_ids": [str(event_id) for event_id in events.get("offered_event_ids", []) if str(event_id)],
            "pending_offers": with_choice_numbers(pending_offers),
            "last_synced_at": events.get("last_synced_at"),
            "last_offered_at": events.get("last_offered_at"),
            "last_status": events.get("last_status"),
            "last_message": events.get("last_message", ""),
        },
    }
