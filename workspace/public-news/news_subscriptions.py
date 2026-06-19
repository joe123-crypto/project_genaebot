#!/usr/bin/env python3
"""Manage public personalized-news WhatsApp subscriptions."""

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
        "OPENCLAW_PUBLIC_NEWS_REGISTRY",
        "/home/joseph/.openclaw/runtime/public-news/subscriptions.json",
    )
)
DEFAULT_OPENCLAW_CONFIG = pathlib.Path("/home/joseph/.openclaw/openclaw.json")
DEFAULT_INVITES_STORE = pathlib.Path(
    os.environ.get(
        "OPENCLAW_INVITES_PATH",
        "/home/joseph/.openclaw/runtime/public-service-gate/invites.json",
    )
)
LEGACY_INVITES_STORE = pathlib.Path("/home/joseph/.openclaw/workspace/invites/invites.json")


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


def normalize_topics(values: list[str]) -> list[str]:
    parts = []
    for value in values or []:
        raw = str(value or "")
        raw = re.sub(r"\s+(?:and|&)\s+", ",", raw, flags=re.IGNORECASE)
        for piece in re.split(r",|\n|;|\s+\|\s+", raw):
            topic = re.sub(r"\s+", " ", piece).strip(" .:-")
            if topic:
                parts.append(topic)
    deduped = []
    seen = set()
    for topic in parts:
        key = topic.casefold()
        if key not in seen:
            seen.add(key)
            deduped.append(topic)
    return deduped[:12]


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
    topics = normalize_topics(args.topics)
    if not topics:
        raise ValueError("at least one topic is required")

    def mutate(payload: dict) -> dict:
        subscribers = payload["subscribers"]
        record = subscriber_record(phone, subscribers.get(phone))
        news = dict(record.get("services", {}).get("news", {}))
        news.update(
            {
                "enabled": True,
                "topics": topics,
                "frequency": args.frequency,
                "language": args.language,
                "updated_at": now_iso(),
            }
        )
        news.setdefault("created_at", now_iso())
        news.setdefault("last_sent_at", None)
        news.setdefault("last_status", None)
        record["services"]["news"] = news
        subscribers[phone] = record
        return sanitize_record(record)

    return with_locked_registry(args.registry, mutate)


def cmd_disable(args: argparse.Namespace) -> dict:
    phone = normalize_phone(args.phone)

    def mutate(payload: dict) -> dict:
        subscribers = payload["subscribers"]
        record = subscriber_record(phone, subscribers.get(phone))
        news = dict(record.get("services", {}).get("news", {}))
        news["enabled"] = False
        news["updated_at"] = now_iso()
        record["services"]["news"] = news
        subscribers[phone] = record
        return sanitize_record(record)

    return with_locked_registry(args.registry, mutate)


def load_unlocked_invite_members(invites_store: pathlib.Path) -> set[str]:
    if not invites_store.exists() and invites_store == DEFAULT_INVITES_STORE and LEGACY_INVITES_STORE.exists():
        invites_store = LEGACY_INVITES_STORE
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
    news = dict(record.get("services", {}).get("news", {}))
    return {
        "phone": record.get("phone"),
        "phone_hash": record.get("phone_hash"),
        "news": {
            "enabled": bool(news.get("enabled")),
            "topics": list(news.get("topics", [])),
            "frequency": news.get("frequency", "daily"),
            "language": news.get("language", "English"),
            "last_sent_at": news.get("last_sent_at"),
            "last_status": news.get("last_status"),
        },
    }


def cmd_list_enabled(args: argparse.Namespace) -> dict:
    payload = load_json(args.registry)
    allowlisted = load_allowlisted(args.openclaw_config, args.invites_store) if args.allowlisted_only else None
    subscribers = []
    skipped = 0
    for record in payload.get("subscribers", {}).values():
        news = record.get("services", {}).get("news", {})
        if not news.get("enabled"):
            continue
        phone = record.get("phone", "")
        if allowlisted is not None and phone not in allowlisted:
            skipped += 1
            continue
        subscribers.append(sanitize_record(record))
    subscribers.sort(key=lambda item: item.get("phone_hash") or "")
    return {"status": "ok", "subscribers": subscribers, "skipped_not_allowlisted": skipped}


def cmd_record_delivery(args: argparse.Namespace) -> dict:
    phone = normalize_phone(args.phone)

    def mutate(payload: dict) -> dict:
        subscribers = payload["subscribers"]
        if phone not in subscribers:
            raise ValueError("subscriber not found")
        record = subscribers[phone]
        news = record.setdefault("services", {}).setdefault("news", {})
        news["last_sent_at"] = now_iso()
        news["last_status"] = args.status
        if args.message:
            news["last_message"] = args.message[:240]
        news["updated_at"] = now_iso()
        record["updated_at"] = now_iso()
        return sanitize_record(record)

    return with_locked_registry(args.registry, mutate)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=pathlib.Path, default=DEFAULT_REGISTRY)
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser("register")
    register.add_argument("--phone", required=True)
    register.add_argument("--frequency", default="daily")
    register.add_argument("--language", default="English")
    register.add_argument("topics", nargs="+")
    register.set_defaults(func=cmd_register)

    disable = sub.add_parser("disable")
    disable.add_argument("--phone", required=True)
    disable.set_defaults(func=cmd_disable)

    list_enabled = sub.add_parser("list-enabled")
    list_enabled.add_argument("--allowlisted-only", action="store_true")
    list_enabled.add_argument("--openclaw-config", type=pathlib.Path, default=DEFAULT_OPENCLAW_CONFIG)
    list_enabled.add_argument("--invites-store", type=pathlib.Path, default=DEFAULT_INVITES_STORE)
    list_enabled.set_defaults(func=cmd_list_enabled)

    record = sub.add_parser("record-delivery")
    record.add_argument("--phone", required=True)
    record.add_argument("--status", required=True, choices=["delivered", "failed", "skipped"])
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
