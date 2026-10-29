#!/usr/bin/env python3
"""Emit one bounded, sanitized startup result for CI; never send a message."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time

PREFIX = "GENAIE_RELEASE_READY "
REQUIRED_ACCOUNTS = {"default"}


def inspect(state, marker, channels, environment):
    config = json.loads((state / "openclaw.json").read_text())
    whatsapp = config.get("channels", {}).get("whatsapp", {})
    expected = {name for name, entry in whatsapp.get("accounts", {}).items()
                if entry.get("enabled", True) and whatsapp.get("enabled", True)}
    accounts = channels.get("channelAccounts", {}).get("whatsapp", [])
    connected = {entry.get("accountId") for entry in accounts if entry.get("connected") is True}
    if not REQUIRED_ACCOUNTS.issubset(expected):
        raise ValueError("required main WhatsApp account is missing or disabled")
    if not REQUIRED_ACCOUNTS.issubset(connected):
        raise ValueError("required main WhatsApp account has not connected")
    disconnected = expected - connected
    with closing(sqlite3.connect(f"file:{state / 'state/openclaw.sqlite'}?mode=ro", uri=True)) as database:
        jobs = [row[0] for row in database.execute("SELECT name FROM cron_jobs WHERE enabled=1")
                if row[0].endswith(("-promo", "-feed"))]
    source = re.search(r"^Source commit: ([0-9a-f]{40})$", marker.read_text(), re.MULTILINE)
    return {
        "ready": True,
        "deploymentId": environment.get("RAILWAY_DEPLOYMENT_ID"),
        "sourceCommit": source.group(1) if source else "unknown",
        "transport": environment.get("GENAIE_CAMPAIGN_TRANSPORT", "cli"),
        "catalogIdleRetirement": environment.get("GENAIE_CATALOG_IDLE_RETIREMENT", "0"),
        "historyIdleRetirement": environment.get("GENAIE_HISTORY_IDLE_RETIREMENT", "0"),
        "mallocArenaMax": environment.get("MALLOC_ARENA_MAX", "0"),
        "expectedAccounts": sorted(expected), "connectedAccounts": sorted(connected),
        "requiredAccounts": sorted(REQUIRED_ACCOUNTS),
        "disconnectedAccounts": sorted(disconnected), "degraded": bool(disconnected),
        "enabledJobs": sorted(jobs),
        "bindingsHash": hashlib.sha256(json.dumps(config.get("bindings", []), sort_keys=True).encode()).hexdigest(),
    }


def main():
    state = Path(os.environ.get("OPENCLAW_STATE_DIR", "/home/joseph/.openclaw"))
    marker = Path("/opt/live-source/.OPENCLAW_GENERATED")
    environment = dict(os.environ)
    environment.pop("OPENCLAW_HOME", None)
    environment["OPENCLAW_GATEWAY_TOKEN"] = environment.get("GATEWAY_AUTH_TOKEN", "")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(["openclaw", "channels", "status", "--json"],
                                    env=environment, capture_output=True, text=True,
                                    check=True, timeout=min(15, max(1, deadline - time.monotonic())))
            report = inspect(state, marker, json.loads(result.stdout), environment)
            print(PREFIX + json.dumps(report), flush=True)
            return
        except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
            time.sleep(min(5, max(0, deadline - time.monotonic())))
    print(PREFIX + json.dumps({"ready": False,
          "deploymentId": environment.get("RAILWAY_DEPLOYMENT_ID"),
          "reason": "startup verification timed out"}), flush=True)


if __name__ == "__main__":
    main()
