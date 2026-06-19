#!/usr/bin/env python3
"""Manage single-use peer invite codes for the public WhatsApp service desk.

A public user, admin, or pending user creates an invite code; the gate plugin
turns it into a wa.me one-tap link. A not-yet-allowlisted person who sends
"JOIN-<code>" redeems it once, which activates their WhatsApp number in a
pending state. The inviter is unlocked when that redemption succeeds.

The store is a small JSON file keyed by uppercase code, plus a phone-keyed member
state map. Normal "this code is invalid / already used" outcomes are reported as
JSON on stdout with exit 0 so the caller can parse them; only usage/IO errors exit
non-zero.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path


RUNTIME_STORE_PATH = Path.home() / ".openclaw" / "runtime" / "public-service-gate" / "invites.json"
LEGACY_STORE_PATH = Path.home() / ".openclaw" / "workspace" / "invites" / "invites.json"
DEFAULT_STORE_PATH = Path(os.environ.get("OPENCLAW_INVITES_PATH", str(RUNTIME_STORE_PATH)))
# Crockford-ish base32 without ambiguous chars (no 0/O/1/I).
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8


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


def normalize_code(value: str) -> str:
    code = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    if not code:
        raise InviteError("code is empty")
    return code


def load_store(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {"invites": {}, "members": {}}
    except json.JSONDecodeError as exc:
        raise InviteError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("invites"), dict):
        raise InviteError(f"unexpected store shape in {path}")
    if not isinstance(data.get("members", {}), dict):
        raise InviteError(f"unexpected members shape in {path}")
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


def gen_code(existing: dict) -> str:
    for _ in range(100):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        if code not in existing:
            return code
    raise InviteError("could not generate a unique code")


def cmd_create(args: argparse.Namespace, store: dict) -> tuple[dict, bool]:
    inviter = normalize_phone(args.inviter)
    invites = store["invites"]
    code = gen_code(invites)
    invites[code] = {
        "code": code,
        "inviter": inviter,
        "status": "created",
        "createdAt": utc_now_iso(),
    }
    return {"code": code, "inviter": inviter, "status": "created"}, True


def cmd_redeem(args: argparse.Namespace, store: dict) -> tuple[dict, bool]:
    phone = normalize_phone(args.phone)
    code = normalize_code(args.code)
    record = store["invites"].get(code)
    if not record:
        return {"ok": False, "reason": "invalid"}, False
    status = record.get("status")
    if status != "created":
        reason = "already_used" if status == "used" else "revoked" if status == "revoked" else "invalid"
        return {"ok": False, "reason": reason}, False
    inviter = normalize_phone(record.get("inviter"))
    if inviter == phone:
        return {"ok": False, "reason": "self_invite"}, False
    record["status"] = "used"
    record["redeemedBy"] = phone
    record["redeemedAt"] = utc_now_iso()
    members = store.setdefault("members", {})
    member = members.get(phone)
    if not isinstance(member, dict):
        member = {}
        members[phone] = member
    if member.get("status") != "unlocked":
        member["phone"] = phone
        member["status"] = "pending"
        member.setdefault("activatedAt", record["redeemedAt"])
        member.setdefault("invitedBy", inviter)
        member.setdefault("inviteCode", code)

    inviter_unlocked = False
    inviter_member = members.get(inviter)
    if isinstance(inviter_member, dict) and inviter_member.get("status") != "unlocked":
        inviter_member["phone"] = inviter
        inviter_member["status"] = "unlocked"
        inviter_member["unlockedAt"] = record["redeemedAt"]
        inviter_member["unlockedBy"] = phone
        inviter_unlocked = True

    return {
        "ok": True,
        "code": code,
        "inviter": inviter,
        "redeemedBy": phone,
        "redeemedStatus": member.get("status"),
        "inviterUnlocked": inviter_unlocked,
    }, True


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


def merge_store(store: dict, source: dict) -> tuple[dict, bool]:
    invites_added = 0
    members_added = 0
    members_updated = 0
    invites = store.setdefault("invites", {})
    for raw_code, record in source.get("invites", {}).items():
        if not isinstance(record, dict):
            continue
        code = normalize_code(record.get("code") or raw_code)
        if code in invites:
            continue
        next_record = dict(record)
        next_record["code"] = code
        invites[code] = next_record
        invites_added += 1

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
        "invitesAdded": invites_added,
        "membersAdded": members_added,
        "membersUpdated": members_updated,
    }, bool(invites_added or members_added or members_updated)


def cmd_merge_store(args: argparse.Namespace, store: dict) -> tuple[dict, bool]:
    source = load_store(Path(args.from_file).expanduser())
    return merge_store(store, source)


def cmd_revoke(args: argparse.Namespace, store: dict) -> tuple[dict, bool]:
    code = normalize_code(args.code)
    record = store["invites"].get(code)
    if not record:
        return {"ok": False, "reason": "invalid"}, False
    if record.get("status") == "used":
        return {"ok": False, "reason": "already_used"}, False
    if record.get("status") == "revoked":
        return {"ok": True, "code": code, "status": "revoked"}, False
    record["status"] = "revoked"
    record["revokedAt"] = utc_now_iso()
    return {"ok": True, "code": code, "status": "revoked"}, True


def cmd_list(args: argparse.Namespace, store: dict) -> tuple[dict, bool]:
    invites = list(store["invites"].values())
    invites.sort(key=lambda r: r.get("createdAt", ""), reverse=True)
    members = list(store.get("members", {}).values())
    members.sort(key=lambda r: r.get("activatedAt", ""), reverse=True)
    return {"count": len(invites), "invites": invites, "members": members}, False


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
    parser = argparse.ArgumentParser(description="Manage single-use WhatsApp peer invite codes.")
    parser.add_argument("--store", default=str(DEFAULT_STORE_PATH), help="Path to invites.json")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="Create a single-use invite code")
    create.add_argument("--inviter", required=True, help="Phone of the existing user creating the invite")
    create.set_defaults(func=cmd_create)

    redeem = sub.add_parser("redeem", help="Redeem an invite code for a new phone (single-use)")
    redeem.add_argument("--code", required=True)
    redeem.add_argument("--phone", required=True, help="Phone of the new user redeeming the code")
    redeem.set_defaults(func=cmd_redeem)

    status = sub.add_parser("status", help="Check pending/unlocked invite status for a phone")
    status.add_argument("--phone", required=True)
    status.set_defaults(func=cmd_status)

    grandfather = sub.add_parser(
        "grandfather", help="Unlock a phone directly (grandfather a pre-invite user)"
    )
    grandfather.add_argument("--phone", required=True)
    grandfather.set_defaults(func=cmd_grandfather)

    merge = sub.add_parser("merge-store", help="Add missing invites and unlocked members from another store")
    merge.add_argument("--from", dest="from_file", required=True, help="Path to a source invites.json")
    merge.set_defaults(func=cmd_merge_store)

    revoke = sub.add_parser("revoke", help="Revoke an unused invite code")
    revoke.add_argument("--code", required=True)
    revoke.set_defaults(func=cmd_revoke)

    listp = sub.add_parser("list", help="List invite codes")
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
