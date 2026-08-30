#!/usr/bin/env python3
"""Health probe for the isolated Job Scout WhatsApp Channel listener."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

from jobscout.config import CHANNEL_DB_PATH, CHANNEL_HEALTH_PATH


DEFAULT_HEALTH = CHANNEL_HEALTH_PATH
DEFAULT_DB = CHANNEL_DB_PATH


def parse_time(value: object) -> datetime | None:
  try:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
  except (TypeError, ValueError):
    return None


def check(health_path: Path, db_path: Path, max_age_seconds: int) -> dict:
  errors: list[str] = []
  try:
    health = json.loads(health_path.read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError) as err:
    return {"ok": False, "errors": [f"health file unavailable: {err}"]}
  heartbeat = parse_time(health.get("lastHeartbeatAt"))
  age = None if heartbeat is None else (datetime.now(timezone.utc) - heartbeat).total_seconds()
  if heartbeat is None or age is None or age > max_age_seconds:
    errors.append("listener heartbeat is stale")
  if health.get("connected") is not True:
    errors.append("listener is disconnected")
  if health.get("authenticated") is not True:
    errors.append("listener authentication is unavailable")
  raw_channels = health.get("channels")
  channels = raw_channels if isinstance(raw_channels, list) else []
  if channels:
    for channel in channels:
      source = str(channel.get("source") or "unknown channel") if isinstance(channel, dict) else "unknown channel"
      channel_jid = str(channel.get("channelJid") or "") if isinstance(channel, dict) else ""
      if not channel_jid.endswith("@newsletter"):
        errors.append(f"{source} channel JID is unresolved")
      if not str(channel.get("lastSubscriptionAt") or ""):
        errors.append(f"{source} channel subscription is unavailable")
      if channel.get("lastError"):
        errors.append(f"{source} channel error: {channel['lastError']}")
  else:
    channel_jid = str(health.get("channelJid") or "")
    if not channel_jid.endswith("@newsletter"):
      errors.append("WhatsApp channel JID is unresolved")
  counts: dict[str, int] = {}
  media_bytes = 0
  try:
    conn = sqlite3.connect(db_path, timeout=5)
    counts = {str(status): int(count) for status, count in conn.execute("SELECT status, COUNT(*) FROM posts GROUP BY status")}
    media_bytes = int(conn.execute("SELECT COALESCE(SUM(media_bytes),0) FROM posts WHERE raw_purged=0").fetchone()[0])
    conn.close()
  except (OSError, sqlite3.Error) as err:
    errors.append(f"listener database unavailable: {err}")
  return {
    "ok": not errors,
    "errors": errors,
    "connected": health.get("connected") is True,
    "authenticated": health.get("authenticated") is True,
    "channelJid": str(health.get("channelJid") or "") or None,
    "channelName": health.get("channelName"),
    "channels": channels,
    "heartbeatAgeSeconds": None if age is None else round(age, 1),
    "lastSubscriptionAt": health.get("lastSubscriptionAt"),
    "reconnectCount": int(health.get("reconnectCount") or 0),
    "counts": counts,
    "mediaBytes": media_bytes,
  }


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(prog="job-scout-channel-health")
  parser.add_argument("--max-age-seconds", type=int, default=120)
  args = parser.parse_args(argv)
  result = check(DEFAULT_HEALTH, DEFAULT_DB, max(1, args.max_age_seconds))
  print(json.dumps(result, indent=2, sort_keys=True))
  return 0 if result["ok"] else 1


if __name__ == "__main__":
  sys.exit(main())
