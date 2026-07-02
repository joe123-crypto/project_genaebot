#!/usr/bin/env python3
"""Private, source-specific delivery journal for Webetu meal summaries."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sys


ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
PHONE_RE = re.compile(r"\+[0-9][0-9 ()-]{7,}[0-9]")


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def journal_dir(value: str) -> pathlib.Path:
    path = pathlib.Path(value)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    resolved = path / "resolved"
    resolved.mkdir(exist_ok=True, mode=0o700)
    os.chmod(resolved, 0o700)
    return path


def validate_id(value: str) -> str:
    if not ID_RE.fullmatch(value):
        raise SystemExit("invalid delivery id")
    return value


def pending_path(base: pathlib.Path, delivery_id: str) -> pathlib.Path:
    return base / f"{validate_id(delivery_id)}.json"


def load_pending(base: pathlib.Path, delivery_id: str) -> tuple[pathlib.Path, dict]:
    path = pending_path(base, delivery_id)
    if not path.is_file():
        raise SystemExit(f"pending delivery not found: {delivery_id}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("deliveryId") != delivery_id:
        raise SystemExit("delivery id does not match journal record")
    return path, payload


def atomic_write(path: pathlib.Path, payload: dict) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("x", encoding="utf-8") as handle:
            os.chmod(temp, 0o600)
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        temp.unlink(missing_ok=True)


def cmd_prepare(args: argparse.Namespace) -> None:
    base = journal_dir(args.directory)
    path = pending_path(base, args.delivery_id)
    if path.exists() or (base / "resolved" / path.name).exists():
        raise SystemExit(f"delivery id already exists: {args.delivery_id}")
    message = pathlib.Path(args.message_file).read_text(encoding="utf-8")
    payload = {
        "version": 1,
        "deliveryId": args.delivery_id,
        "createdAt": utc_now(),
        "status": "prepared",
        "userId": args.user_id,
        "phoneHash": args.phone_hash,
        "source": args.source,
        "dates": args.date,
        "message": message,
        "attemptStartedAt": None,
        "failedAt": None,
        "deliveredAt": None,
        "lastError": None,
    }
    atomic_write(path, payload)


def cmd_mark_attempt(args: argparse.Namespace) -> None:
    base = journal_dir(args.directory)
    path, payload = load_pending(base, args.delivery_id)
    payload["status"] = "send_attempt_started"
    payload["attemptStartedAt"] = utc_now()
    atomic_write(path, payload)


def cmd_mark_failed(args: argparse.Namespace) -> None:
    base = journal_dir(args.directory)
    path, payload = load_pending(base, args.delivery_id)
    error = pathlib.Path(args.error_file).read_text(encoding="utf-8", errors="replace")
    error = PHONE_RE.sub("[redacted-phone]", " ".join(error.split()))[:1000]
    payload["status"] = "failed"
    payload["failedAt"] = utc_now()
    payload["lastError"] = error
    atomic_write(path, payload)


def cmd_mark_delivered(args: argparse.Namespace) -> None:
    base = journal_dir(args.directory)
    path, payload = load_pending(base, args.delivery_id)
    payload["status"] = "delivered"
    payload["deliveredAt"] = utc_now()
    payload["lastError"] = None
    atomic_write(path, payload)
    os.replace(path, base / "resolved" / path.name)


def cmd_list(args: argparse.Namespace) -> None:
    base = journal_dir(args.directory)
    print("delivery_id\tcreated_at\tstatus\tphone_hash\tdates")
    for path in sorted(base.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        dates = ",".join(str(value) for value in payload.get("dates", []))
        fields = (
            payload.get("deliveryId", ""),
            payload.get("createdAt", ""),
            payload.get("status", ""),
            payload.get("phoneHash", ""),
            dates,
        )
        print("\t".join(str(value).replace("\t", " ") for value in fields))


def cmd_extract(args: argparse.Namespace) -> None:
    base = journal_dir(args.directory)
    _, payload = load_pending(base, args.delivery_id)
    pathlib.Path(args.message_file).write_text(str(payload.get("message", "")), encoding="utf-8")
    os.chmod(args.message_file, 0o600)
    fields = (
        payload.get("userId", ""),
        payload.get("phoneHash", ""),
        payload.get("source", ""),
    )
    if not fields[0] or not fields[1]:
        raise SystemExit("journal record is missing user identity metadata")
    print("\t".join(str(value).replace("\t", " ") for value in fields))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    subparsers = result.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--directory", required=True)
    prepare.add_argument("--delivery-id", required=True)
    prepare.add_argument("--user-id", required=True)
    prepare.add_argument("--phone-hash", required=True)
    prepare.add_argument("--source", required=True)
    prepare.add_argument("--date", action="append", default=[])
    prepare.add_argument("--message-file", required=True)
    prepare.set_defaults(func=cmd_prepare)

    for name, func in (
        ("mark-attempt", cmd_mark_attempt),
        ("mark-delivered", cmd_mark_delivered),
    ):
        command = subparsers.add_parser(name)
        command.add_argument("--directory", required=True)
        command.add_argument("--delivery-id", required=True)
        command.set_defaults(func=func)

    failed = subparsers.add_parser("mark-failed")
    failed.add_argument("--directory", required=True)
    failed.add_argument("--delivery-id", required=True)
    failed.add_argument("--error-file", required=True)
    failed.set_defaults(func=cmd_mark_failed)

    listing = subparsers.add_parser("list")
    listing.add_argument("--directory", required=True)
    listing.set_defaults(func=cmd_list)

    extract = subparsers.add_parser("extract")
    extract.add_argument("--directory", required=True)
    extract.add_argument("--delivery-id", required=True)
    extract.add_argument("--message-file", required=True)
    extract.set_defaults(func=cmd_extract)

    return result


def main() -> int:
    args = parser().parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
