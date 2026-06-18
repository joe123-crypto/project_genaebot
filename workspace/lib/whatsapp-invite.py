#!/usr/bin/env python3
"""Manage single-use peer invite codes for the public WhatsApp service desk.

A public user (or admin) creates an invite code; the gate plugin turns it into a
wa.me one-tap link. A not-yet-allowlisted person who sends "JOIN-<code>" redeems
it once, which lets the gate add them to the public allowlist.

The store is a small JSON file keyed by uppercase code. Normal "this code is
invalid / already used" outcomes are reported as JSON on stdout with exit 0 so
the caller can parse them; only usage/IO errors exit non-zero.
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


DEFAULT_STORE_PATH = Path(
    os.environ.get(
        "OPENCLAW_INVITES_PATH",
        str(Path.home() / ".openclaw" / "workspace" / "invites" / "invites.json"),
    )
)
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
        return {"invites": {}}
    except json.JSONDecodeError as exc:
        raise InviteError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("invites"), dict):
        raise InviteError(f"unexpected store shape in {path}")
    return data


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
    record["status"] = "used"
    record["redeemedBy"] = phone
    record["redeemedAt"] = utc_now_iso()
    return {"ok": True, "code": code, "inviter": record.get("inviter"), "redeemedBy": phone}, True


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
    return {"count": len(invites), "invites": invites}, False


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
    store = load_store(store_path)
    result, changed = args.func(args, store)
    if changed:
        save_store_atomic(store_path, store)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InviteError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
