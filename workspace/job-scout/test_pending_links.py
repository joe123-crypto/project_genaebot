from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import stat
import tempfile
import unittest

from jobscout.integrations import pending_links


def iso_after(delta: timedelta) -> str:
  return (datetime.now(timezone.utc) + delta).isoformat().replace("+00:00", "Z")


class PendingLinksTests(unittest.TestCase):
  def test_store_get_replace_and_remove_use_private_temp_state(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      cache = Path(tmp) / "account-link" / "pending-links.json"
      values = pending_links.criteria(
        phone="+263 77 123 4567",
        phone_hash="abc123",
        service="job-scout",
        purpose="profile",
        next_path="/jobs",
      )
      first = pending_links.store_pending_link({
        **values,
        "setupUrl": "https://example.test/first",
        "expiresAt": iso_after(timedelta(hours=1)),
      }, cache)
      self.assertFalse(first["cached"])
      self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o600)
      self.assertEqual(pending_links.get_pending_link(values, cache)["setupUrl"], "https://example.test/first")

      pending_links.store_pending_link({
        **values,
        "setupUrl": "https://example.test/replaced",
        "expiresAt": iso_after(timedelta(hours=2)),
      }, cache)
      self.assertEqual(len(pending_links.read_links(cache)), 1)
      self.assertEqual(pending_links.get_pending_link(values, cache)["setupUrl"], "https://example.test/replaced")
      self.assertEqual(pending_links.remove_pending_links(phone=values["phone"], service="job-scout", cache_path=cache), 1)
      self.assertIsNone(pending_links.get_pending_link(values, cache))

  def test_expired_and_malformed_records_are_unusable(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      cache = Path(tmp) / "pending.json"
      cache.write_text(json.dumps({
        "version": 1,
        "links": [
          {"setupUrl": "https://example.test/expired", "expiresAt": iso_after(timedelta(seconds=-1))},
          {"setupUrl": "", "expiresAt": iso_after(timedelta(hours=1))},
          "invalid",
        ],
      }), encoding="utf-8")
      self.assertEqual(pending_links.load_usable_links(cache), [])
      self.assertEqual(json.loads(cache.read_text(encoding="utf-8")), {"links": [], "version": 1})

  def test_missing_and_invalid_cache_are_empty(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      cache = Path(tmp) / "pending.json"
      self.assertEqual(pending_links.read_links(cache), [])
      cache.write_text("not-json\n", encoding="utf-8")
      self.assertEqual(pending_links.read_links(cache), [])


if __name__ == "__main__":
  unittest.main()
