from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import channel_health


class ChannelHealthTests(unittest.TestCase):
  def test_healthy_listener_reports_queue_metrics(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      health = root / "health.json"
      database = root / "posts.sqlite3"
      health.write_text(json.dumps({
        "connected": True,
        "authenticated": True,
        "channelJid": "123@newsletter",
        "channelName": "ZimLinks",
        "lastHeartbeatAt": datetime.now(timezone.utc).isoformat(),
        "lastSubscriptionAt": datetime.now(timezone.utc).isoformat(),
      }), encoding="utf-8")
      conn = sqlite3.connect(database)
      conn.execute("CREATE TABLE posts(status TEXT, media_bytes INTEGER, raw_purged INTEGER)")
      conn.execute("INSERT INTO posts VALUES('pending', 42, 0)")
      conn.commit()
      conn.close()
      result = channel_health.check(health, database, 120)
    self.assertTrue(result["ok"])
    self.assertEqual(result["counts"], {"pending": 1})
    self.assertEqual(result["mediaBytes"], 42)

  def test_stale_disconnected_listener_fails(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      health = root / "health.json"
      database = root / "posts.sqlite3"
      health.write_text(json.dumps({
        "connected": False,
        "authenticated": False,
        "lastHeartbeatAt": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
      }), encoding="utf-8")
      result = channel_health.check(health, database, 120)
    self.assertFalse(result["ok"])
    self.assertIn("listener heartbeat is stale", result["errors"])
    self.assertIn("listener is disconnected", result["errors"])


if __name__ == "__main__":
  unittest.main()
