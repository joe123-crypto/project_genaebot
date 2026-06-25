#!/usr/bin/env python3
"""Manage public local-event-scouting WhatsApp subscriptions."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import re
import sys
import tempfile
from datetime import datetime, timezone


DEFAULT_REGISTRY = pathlib.Path(
    os.environ.get(
        "OPENCLAW_PUBLIC_EVENTS_REGISTRY",
        "/home/joseph/.openclaw/runtime/public-events/subscriptions.json",
    )
)
DEFAULT_OPENCLAW_CONFIG = pathlib.Path("/home/joseph/.openclaw/openclaw.json")
DEFAULT_INVITES_STORE = pathlib.Path(
    os.environ.get(
        "OPENCLAW_INVITES_PATH",
        "/home/joseph/.openclaw/runtime/public-service-gate/invites.json",
    )
)

# Stored as minutes-before-start to match the Phase A calendar API reminderOverrides.
DEFAULT_REMINDER_MINUTES = [1440, 120]
DEFAULT_DELIVERY_MODE = "approval"
DELIVERY_MODES = {"approval", "auto_add"}
MAX_STORED_EVENT_IDS = 500
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


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_phone(value: str) -> str:
    value = str(value or "").strip()
    digits = re.sub(r"\D", "", value)
    if not digits:
        raise ValueError("phone is required")
    normalized = f"+{digits}"
    if len(normalized) < 9 or len(normalized) > 16:
        raise ValueError("phone must normalize to 9-16 digits")
    return normalized


def phone_hash(phone: str) -> str:
    return hashlib.sha256(f"whatsapp:{phone}".encode("utf-8")).hexdigest()[:12]


def load_json(path: pathlib.Path) -> dict:
    if not path.exists():
        return {"version": 1, "subscribers": {}}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {"version": 1, "subscribers": {}}
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("registry root must be a JSON object")
    payload.setdefault("version", 1)
    payload.setdefault("subscribers", {})
    if not isinstance(payload["subscribers"], dict):
        raise ValueError("registry subscribers must be a JSON object")
    return payload


def write_json(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def with_locked_registry(path: pathlib.Path, mutate):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        payload = load_json(path)
        result = mutate(payload)
        write_json(path, payload)
        return result


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


def cmd_register(args: argparse.Namespace) -> dict:
    phone = normalize_phone(args.phone)
    user_id = str(args.user_id or "").strip()
    if not user_id:
        raise ValueError("user_id is required")
    categories = normalize_categories(args.category)
    location = re.sub(r"\s+", " ", str(args.location or "")).strip()
    reminders = normalize_reminders(args.reminder_min) or list(DEFAULT_REMINDER_MINUTES)
    delivery_mode = normalize_delivery_mode(args.delivery_mode)

    def mutate(payload: dict) -> dict:
        subscribers = payload["subscribers"]
        record = subscriber_record(phone, subscribers.get(phone))
        events = dict(record.get("services", {}).get("events", {}))
        events.update(
            {
                "enabled": True,
                "user_id": user_id,
                "categories": categories,
                "location": location,
                "reminder_minutes": reminders,
                "delivery_mode": delivery_mode,
                "updated_at": now_iso(),
            }
        )
        events.setdefault("created_at", now_iso())
        events.setdefault("synced_event_ids", [])
        events.setdefault("offered_event_ids", [])
        events.setdefault("pending_offers", [])
        events.setdefault("last_synced_at", None)
        events.setdefault("last_status", None)
        record["services"]["events"] = events
        subscribers[phone] = record
        return sanitize_record(record)

    return with_locked_registry(args.registry, mutate)


def cmd_disable(args: argparse.Namespace) -> dict:
    phone = normalize_phone(args.phone)

    def mutate(payload: dict) -> dict:
        subscribers = payload["subscribers"]
        record = subscriber_record(phone, subscribers.get(phone))
        events = dict(record.get("services", {}).get("events", {}))
        events["enabled"] = False
        events["updated_at"] = now_iso()
        record["services"]["events"] = events
        subscribers[phone] = record
        return sanitize_record(record)

    return with_locked_registry(args.registry, mutate)


def cmd_get(args: argparse.Namespace) -> dict:
    phone = normalize_phone(args.phone)
    payload = load_json(args.registry)
    record = payload.get("subscribers", {}).get(phone)
    if not record:
        record = {
            "phone": phone,
            "phone_hash": phone_hash(phone),
            "services": {"events": {}},
        }
    return sanitize_record(record)


def cmd_mark_synced(args: argparse.Namespace) -> dict:
    phone = normalize_phone(args.phone)
    event_ids = [eid for eid in (str(e).strip() for e in (args.event_id or [])) if eid]
    if not event_ids:
        raise ValueError("at least one --event-id is required")

    def mutate(payload: dict) -> dict:
        subscribers = payload["subscribers"]
        if phone not in subscribers:
            raise ValueError("subscriber not found")
        record = subscribers[phone]
        events = record.setdefault("services", {}).setdefault("events", {})
        synced = list(events.get("synced_event_ids", []))
        seen = set(synced)
        for eid in event_ids:
            if eid not in seen:
                seen.add(eid)
                synced.append(eid)
        events["synced_event_ids"] = synced
        events["last_synced_at"] = now_iso()
        events["updated_at"] = now_iso()
        record["updated_at"] = now_iso()
        return sanitize_record(record)

    return with_locked_registry(args.registry, mutate)


def cmd_record_offers(args: argparse.Namespace) -> dict:
    phone = normalize_phone(args.phone)
    offers = parse_offers_json(args.events_json)
    if not offers:
        raise ValueError("at least one event offer is required")

    def mutate(payload: dict) -> dict:
        subscribers = payload["subscribers"]
        if phone not in subscribers:
            raise ValueError("subscriber not found")
        record = subscribers[phone]
        events = record.setdefault("services", {}).setdefault("events", {})
        pending = normalize_pending_offers(events.get("pending_offers", []))
        pending_seen = {offer["source_id"] for offer in pending}
        synced = set(events.get("synced_event_ids", []))
        offered = [str(event_id) for event_id in events.get("offered_event_ids", []) if str(event_id)]
        offered_seen = set(offered)
        added = []
        for offer in offers:
            source_id = offer["source_id"]
            if source_id in synced or source_id in pending_seen or source_id in offered_seen:
                continue
            pending.append(offer)
            pending_seen.add(source_id)
            offered.append(source_id)
            offered_seen.add(source_id)
            added.append({**offer, "choice_number": len(pending)})
        events["pending_offers"] = pending
        events["offered_event_ids"] = offered[-MAX_STORED_EVENT_IDS:]
        if added:
            events["last_offered_at"] = now_iso()
            events["last_status"] = "offered"
            events["last_message"] = f"offered {len(added)}"
        events["updated_at"] = now_iso()
        record["updated_at"] = now_iso()
        sanitized = sanitize_record(record)
        sanitized["added"] = len(added)
        sanitized["added_offers"] = added
        return sanitized

    return with_locked_registry(args.registry, mutate)


def cmd_clear_pending(args: argparse.Namespace) -> dict:
    phone = normalize_phone(args.phone)
    event_ids = {eid for eid in (str(e).strip() for e in (args.event_id or [])) if eid}
    if not event_ids:
        raise ValueError("at least one --event-id is required")

    def mutate(payload: dict) -> dict:
        subscribers = payload["subscribers"]
        if phone not in subscribers:
            raise ValueError("subscriber not found")
        record = subscribers[phone]
        events = record.setdefault("services", {}).setdefault("events", {})
        pending = normalize_pending_offers(events.get("pending_offers", []))
        remaining = [offer for offer in pending if offer["source_id"] not in event_ids]
        removed = len(pending) - len(remaining)
        events["pending_offers"] = remaining
        events["updated_at"] = now_iso()
        record["updated_at"] = now_iso()
        sanitized = sanitize_record(record)
        sanitized["removed"] = removed
        return sanitized

    return with_locked_registry(args.registry, mutate)


def cmd_record_delivery(args: argparse.Namespace) -> dict:
    phone = normalize_phone(args.phone)

    def mutate(payload: dict) -> dict:
        subscribers = payload["subscribers"]
        if phone not in subscribers:
            raise ValueError("subscriber not found")
        record = subscribers[phone]
        events = record.setdefault("services", {}).setdefault("events", {})
        events["last_status"] = args.status
        if args.message:
            events["last_message"] = args.message[:240]
        events["updated_at"] = now_iso()
        record["updated_at"] = now_iso()
        return sanitize_record(record)

    return with_locked_registry(args.registry, mutate)


def load_unlocked_invite_members(invites_store: pathlib.Path) -> set[str]:
    if not invites_store.exists():
        return set()
    payload = json.loads(invites_store.read_text(encoding="utf-8"))
    members = payload.get("members", {})
    if not isinstance(members, dict):
        return set()
    allowlisted = set()
    for phone, record in members.items():
        if not isinstance(record, dict) or record.get("status") != "unlocked":
            continue
        try:
            allowlisted.add(normalize_phone(record.get("phone") or phone))
        except ValueError:
            continue
    return allowlisted


def load_allowlisted(config_path: pathlib.Path, invites_store: pathlib.Path) -> set[str]:
    normalized = load_unlocked_invite_members(invites_store)
    if not config_path.exists():
        return normalized
    config = json.loads(config_path.read_text(encoding="utf-8"))
    whatsapp = config.get("channels", {}).get("whatsapp", {})
    values = list(whatsapp.get("allowFrom", []) or [])
    direct = whatsapp.get("direct", {})
    if isinstance(direct, dict):
        values.extend(direct.keys())
    public_gate = config.get("plugins", {}).get("entries", {}).get("public-service-gate", {}).get("config", {})
    values.extend(public_gate.get("publicUsers", []) or [])
    values.extend(public_gate.get("whatsappAdminUsers", []) or [])
    for value in values:
        try:
            normalized.add(normalize_phone(value))
        except ValueError:
            continue
    return normalized


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


def cmd_list_enabled(args: argparse.Namespace) -> dict:
    payload = load_json(args.registry)
    allowlisted = load_allowlisted(args.openclaw_config, args.invites_store) if args.allowlisted_only else None
    subscribers = []
    skipped = 0
    for record in payload.get("subscribers", {}).values():
        events = record.get("services", {}).get("events", {})
        if not events.get("enabled"):
            continue
        phone = record.get("phone", "")
        if allowlisted is not None and phone not in allowlisted:
            skipped += 1
            continue
        subscribers.append(sanitize_record(record))
    subscribers.sort(key=lambda item: item.get("phone_hash") or "")
    return {"status": "ok", "subscribers": subscribers, "skipped_not_allowlisted": skipped}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=pathlib.Path, default=DEFAULT_REGISTRY)
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser("register")
    register.add_argument("--phone", required=True)
    register.add_argument("--user-id", required=True, help="Firebase UID for the connected calendar user")
    register.add_argument("--category", action="append", default=[], help="optional category filter; repeat for multiple")
    register.add_argument("--location", default="", help="optional location filter")
    register.add_argument("--reminder-min", action="append", default=[], type=int,
                          help="reminder minutes before start; repeat for multiple")
    register.add_argument("--delivery-mode", default=DEFAULT_DELIVERY_MODE,
                          choices=sorted(DELIVERY_MODES),
                          help="approval asks the user before writing; auto_add writes all matches")
    register.set_defaults(func=cmd_register)

    disable = sub.add_parser("disable")
    disable.add_argument("--phone", required=True)
    disable.set_defaults(func=cmd_disable)

    list_enabled = sub.add_parser("list-enabled")
    list_enabled.add_argument("--allowlisted-only", action="store_true")
    list_enabled.add_argument("--openclaw-config", type=pathlib.Path, default=DEFAULT_OPENCLAW_CONFIG)
    list_enabled.add_argument("--invites-store", type=pathlib.Path, default=DEFAULT_INVITES_STORE)
    list_enabled.set_defaults(func=cmd_list_enabled)

    get = sub.add_parser("get")
    get.add_argument("--phone", required=True)
    get.set_defaults(func=cmd_get)

    mark_synced = sub.add_parser("mark-synced")
    mark_synced.add_argument("--phone", required=True)
    mark_synced.add_argument("--event-id", action="append", default=[],
                             help="synced sourceId, e.g. algeria-exhibitions:8531; repeat for multiple")
    mark_synced.set_defaults(func=cmd_mark_synced)

    record_offers = sub.add_parser("record-offers")
    record_offers.add_argument("--phone", required=True)
    record_offers.add_argument("--events-json", required=True,
                               help="JSON list of pending event snapshots to offer")
    record_offers.set_defaults(func=cmd_record_offers)

    clear_pending = sub.add_parser("clear-pending")
    clear_pending.add_argument("--phone", required=True)
    clear_pending.add_argument("--event-id", action="append", default=[],
                               help="pending sourceId to clear after a confirmed calendar save")
    clear_pending.set_defaults(func=cmd_clear_pending)

    record = sub.add_parser("record-delivery")
    record.add_argument("--phone", required=True)
    record.add_argument("--status", required=True,
                        choices=["delivered", "failed", "skipped", "offered", "saved", "no_matches"])
    record.add_argument("--message", default="")
    record.set_defaults(func=cmd_record_delivery)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = args.func(args)
    except Exception as err:
        print(json.dumps({"status": "error", "message": str(err)}), file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
