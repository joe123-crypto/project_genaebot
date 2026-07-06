#!/usr/bin/env python3
"""Manage public local-event-scouting WhatsApp subscriptions."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys

# Shared registry helpers live in workspace/lib; resolve it relative to this
# script so it works in the deployed workspace and in a git worktree.
LIB_DIR = str((pathlib.Path(__file__).resolve().parent.parent / "lib"))
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)
from phone_utils import normalize_phone, phone_hash
from registry_store import load_json, now_iso, with_locked_registry, write_json
from allowlist import load_allowlisted

# Record-model normalizers/shaping live in the sibling subscription_model module;
# importlib doesn't add this script's dir to sys.path, so add it explicitly.
THIS_DIR = str(pathlib.Path(__file__).resolve().parent)
if THIS_DIR not in sys.path:
    sys.path.insert(0, THIS_DIR)
from subscription_model import (  # noqa: E402
    DEFAULT_DELIVERY_MODE,
    DEFAULT_REMINDER_MINUTES,
    DELIVERY_MODES,
    normalize_categories,
    normalize_delivery_mode,
    normalize_pending_offers,
    normalize_reminders,
    parse_offers_json,
    sanitize_record,
    subscriber_record,
)


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

MAX_STORED_EVENT_IDS = 500


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
