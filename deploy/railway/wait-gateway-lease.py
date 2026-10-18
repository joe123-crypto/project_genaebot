#!/usr/bin/env python3
"""Wait out a gateway owner lease left behind by a container that was killed.

OpenClaw records the running gateway in state/openclaw.sqlite (state_leases,
scope "gateway-owner", key "global") with a 300 s TTL renewed every 30 s. A
clean shutdown deletes the row. A SIGKILL does not: on 2026-10-18 10:09 UTC a
deploy landed while a promo cron run was mid-flight, the old container was
killed two seconds into its drain, and the row stayed. OpenClaw only reclaims a
lease early when the recorded host is this machine and its pid is dead; a new
Railway container has a new hostname, so the lease reads as "unknown" and the
gateway refuses to start ("Another Gateway owner lease is still active") until
the TTL runs out. Railway's restart budget (10 retries, ~12 s apart) ran out
after ~2 min, the deployment was marked CRASHED and the bot stayed down.

Run before `openclaw gateway`: if an unexpired lease is held by another host,
sleep until it expires (re-reading it, so a live owner that keeps renewing is
not waited on forever), capped at --max-wait seconds. Never fatal: any problem
reading the database just lets the gateway start and report for itself.
"""

from __future__ import annotations

import argparse
import json
import socket
import sqlite3
import sys
import time
from pathlib import Path
from typing import Callable, Optional

SCOPE = "gateway-owner"
KEY = "global"
DEFAULT_MAX_WAIT = 330.0  # lease TTL (300 s) plus a margin
POLL_SECONDS = 10.0
EXPIRY_MARGIN_SECONDS = 2.0


def foreign_lease_expiry_ms(db_path: Path, host: str) -> Optional[int]:
    """expires_at (ms) of an unexpired gateway lease held by another host, else None."""
    if not db_path.is_file():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'state_leases'"
        ).fetchone()
        if not has_table:
            return None
        row = conn.execute(
            "SELECT expires_at, payload_json FROM state_leases WHERE scope = ? AND lease_key = ?",
            (SCOPE, KEY),
        ).fetchone()
    finally:
        conn.close()
    if not row or row[0] is None:
        return None
    expires_at = int(row[0])
    if expires_at <= int(time.time() * 1000):
        return None
    try:
        owner_host = (json.loads(row[1] or "null") or {}).get("owner", {}).get("host")
    except (ValueError, AttributeError):
        owner_host = None
    if owner_host == host:
        # Same host: OpenClaw itself reclaims the lease if that pid is dead.
        return None
    return expires_at


def wait_for_lease(
    db_path: Path,
    host: str,
    max_wait: float,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> str:
    deadline = now() + max_wait
    waited = False
    while True:
        expiry_ms = foreign_lease_expiry_ms(db_path, host)
        if expiry_ms is None:
            if waited:
                log("gateway owner lease released; starting gateway")
            return "clear" if not waited else "released"
        remaining = deadline - now()
        if remaining <= 0:
            log("gateway owner lease still held after waiting; starting gateway anyway")
            return "timeout"
        until_expiry = expiry_ms / 1000.0 - now() + EXPIRY_MARGIN_SECONDS
        if not waited:
            log(
                "gateway owner lease held by another host (stale after an unclean stop?); "
                f"waiting up to {int(min(until_expiry, remaining))}s for it to expire"
            )
        waited = True
        sleep(max(0.5, min(until_expiry, remaining, POLL_SECONDS)))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("state_db", type=Path)
    parser.add_argument("--max-wait", type=float, default=DEFAULT_MAX_WAIT)
    args = parser.parse_args(argv)
    try:
        wait_for_lease(args.state_db, socket.gethostname(), args.max_wait)
    except Exception as exc:  # never block the boot on this check
        print(f"gateway lease check skipped: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
