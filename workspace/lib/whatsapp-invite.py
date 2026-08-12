#!/usr/bin/env python3
"""Manage the member store for the public WhatsApp service desk.

Peer invite codes are retired: the admin "allow"/"grandfather" command is the
only way to unlock a WhatsApp number. The store is a small JSON file with a
phone-keyed member state map (a legacy "invites" map from the invite era is
preserved untouched when present). Results are reported as JSON on stdout with
exit 0 so the caller can parse them; only usage/IO errors exit non-zero.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
from pathlib import Path


RUNTIME_STORE_PATH = Path.home() / ".openclaw" / "runtime" / "public-service-gate" / "invites.json"
LEGACY_STORE_PATH = Path.home() / ".openclaw" / "workspace" / "invites" / "invites.json"
DEFAULT_STORE_PATH = Path(os.environ.get("OPENCLAW_INVITES_PATH", str(RUNTIME_STORE_PATH)))


class InviteError(Exception):
    pass


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_phone(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise InviteError("phone number is empty")
    if raw == "*" or "@" in raw:
        raise InviteError(f"not a direct phone number: {raw}")
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 8 or len(digits) > 15:
        raise InviteError(f"invalid phone number length after normalization: {raw}")
    return f"+{digits}"


def load_store(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {"invites": {}, "members": {}}
    except json.JSONDecodeError as exc:
        raise InviteError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("invites", {}), dict):
        raise InviteError(f"unexpected store shape in {path}")
    if not isinstance(data.get("members", {}), dict):
        raise InviteError(f"unexpected members shape in {path}")
    data.setdefault("invites", {})
    data.setdefault("members", {})
    return data


def should_import_legacy_store(path: Path) -> bool:
    return (
        path.expanduser() == RUNTIME_STORE_PATH
        and not path.exists()
        and LEGACY_STORE_PATH.exists()
    )


def save_store_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def cmd_grandfather(args: argparse.Namespace, store: dict) -> tuple[dict, bool]:
    phone = normalize_phone(args.phone)
    members = store.setdefault("members", {})
    member = members.get(phone)
    if isinstance(member, dict) and member.get("status") == "unlocked":
        return {"ok": True, "phone": phone, "status": "unlocked", "changed": False}, False
    now = utc_now_iso()
    if not isinstance(member, dict):
        member = {}
        members[phone] = member
    member["phone"] = phone
    member["status"] = "unlocked"
    member["unlockedAt"] = now
    member.setdefault("activatedAt", now)
    member["source"] = "grandfathered"
    return {"ok": True, "phone": phone, "status": "unlocked", "changed": True}, True


def cmd_request(args: argparse.Namespace, store: dict) -> tuple[dict, bool]:
    """Record a pending access request (lead) for a non-member.

    First contact creates a phone-keyed record with status "pending" so the gate
    keeps the sender blocked until an admin runs "allow". Repeat contacts refresh
    contact metadata but never re-alert (isNew False) and never clobber an
    existing "unlocked"/"pending" status.
    """
    phone = normalize_phone(args.phone)
    members = store.setdefault("members", {})
    member = members.get(phone)
    now = utc_now_iso()
    message = str(getattr(args, "message", "") or "").strip()[:280]

    if isinstance(member, dict) and member.get("status") in ("unlocked", "pending"):
        prior = str(member.get("status"))
        member["lastContactAt"] = now
        member["contactCount"] = int(member.get("contactCount", 0)) + 1
        if message:
            member["lastMessage"] = message
        return {"ok": True, "phone": phone, "status": prior, "changed": True, "isNew": False}, True

    member = {
        "phone": phone,
        "status": "pending",
        "source": "lead",
        "firstContactAt": now,
        "requestedAt": now,
        "lastContactAt": now,
        "contactCount": 1,
    }
    if message:
        member["lastMessage"] = message
    members[phone] = member
    return {"ok": True, "phone": phone, "status": "pending", "changed": True, "isNew": True}, True


def merge_store(store: dict, source: dict) -> tuple[dict, bool]:
    members_added = 0
    members_updated = 0
    members = store.setdefault("members", {})
    for raw_phone, record in source.get("members", {}).items():
        if not isinstance(record, dict) or record.get("status") != "unlocked":
            continue
        try:
            phone = normalize_phone(record.get("phone") or raw_phone)
        except InviteError:
            continue
        existing = members.get(phone)
        if not isinstance(existing, dict):
            next_record = dict(record)
            next_record["phone"] = phone
            next_record["status"] = "unlocked"
            members[phone] = next_record
            members_added += 1
            continue
        if existing.get("status") == "unlocked":
            for key, value in record.items():
                if key not in existing:
                    existing[key] = value
            existing["phone"] = phone
            continue
        for key, value in record.items():
            existing.setdefault(key, value)
        existing["phone"] = phone
        existing["status"] = "unlocked"
        existing["updatedAt"] = utc_now_iso()
        members_updated += 1

    return {
        "ok": True,
        "membersAdded": members_added,
        "membersUpdated": members_updated,
    }, bool(members_added or members_updated)


def cmd_merge_store(args: argparse.Namespace, store: dict) -> tuple[dict, bool]:
    source = load_store(Path(args.from_file).expanduser())
    return merge_store(store, source)


def cmd_list(args: argparse.Namespace, store: dict) -> tuple[dict, bool]:
    members = list(store.get("members", {}).values())
    wanted = getattr(args, "status", None)
    if wanted:
        members = [m for m in members if isinstance(m, dict) and m.get("status") == wanted]
    members.sort(key=lambda r: r.get("requestedAt") or r.get("activatedAt", ""), reverse=True)
    return {"count": len(members), "members": members}, False


def cmd_status(args: argparse.Namespace, store: dict) -> tuple[dict, bool]:
    phone = normalize_phone(args.phone)
    member = store.get("members", {}).get(phone)
    if not isinstance(member, dict):
        return {"phone": phone, "status": "unknown", "pending": False, "unlocked": False}, False
    status = str(member.get("status") or "unknown")
    return {
        "phone": phone,
        "status": status,
        "pending": status == "pending",
        "unlocked": status == "unlocked",
        "member": member,
    }, False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the public WhatsApp member store.")
    parser.add_argument("--store", default=str(DEFAULT_STORE_PATH), help="Path to invites.json")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="Check member status for a phone")
    status.add_argument("--phone", required=True)
    status.set_defaults(func=cmd_status)

    # "allow" is the current admin command; "grandfather" is the legacy spelling.
    for name in ("allow", "grandfather"):
        allow = sub.add_parser(name, help="Unlock a phone directly (admin allow)")
        allow.add_argument("--phone", required=True)
        allow.set_defaults(func=cmd_grandfather)

    request = sub.add_parser("request", help="Record a pending access request (lead)")
    request.add_argument("--phone", required=True)
    request.add_argument("--message", default="", help="Optional first-message snippet")
    request.set_defaults(func=cmd_request)

    merge = sub.add_parser("merge-store", help="Add missing unlocked members from another store")
    merge.add_argument("--from", dest="from_file", required=True, help="Path to a source invites.json")
    merge.set_defaults(func=cmd_merge_store)

    listp = sub.add_parser("list", help="List members")
    listp.add_argument("--status", default=None, help="Filter by status, e.g. pending or unlocked")
    listp.set_defaults(func=cmd_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store_path = Path(args.store).expanduser()
    import_legacy = should_import_legacy_store(store_path)
    if import_legacy:
        store = load_store(LEGACY_STORE_PATH)
    else:
        store = load_store(store_path)
    result, changed = args.func(args, store)
    if import_legacy:
        result.setdefault("migratedFrom", str(LEGACY_STORE_PATH))
    if changed or import_legacy:
        save_store_atomic(store_path, store)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InviteError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
