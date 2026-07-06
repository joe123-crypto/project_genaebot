from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from scrapers import zimlinks


class ZimLinksSourceTests(unittest.TestCase):
  def create_db(self, root: Path) -> Path:
    database = root / "posts.sqlite3"
    conn = sqlite3.connect(database)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(zimlinks.SCHEMA)
    conn.commit()
    conn.close()
    return database

  def insert_post(
    self,
    database: Path,
    post_id: str,
    text: str,
    *,
    media_path: str | None = None,
    posted_at: str | None = None,
    timeout: float = 5,
  ) -> None:
    now = posted_at or datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(database, timeout=timeout)
    conn.execute(
      """
      INSERT INTO posts(
        channel_jid,post_id,posted_at,first_seen_at,last_seen_at,post_url,
        raw_text,media_path,status,raw_purged
      ) VALUES(?,?,?,?,?,?,?,?,?,0)
      """,
      ("123@newsletter", post_id, now, now, now, f"https://whatsapp.com/channel/invite/{post_id}", text, media_path, "pending"),
    )
    conn.commit()
    conn.close()

  def test_deterministic_job_becomes_candidate_without_model(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      database = self.create_db(Path(tmp))
      self.insert_post(database, "10", "VACANCY: Driver\nCompany: Acme Logistics\nEmail: jobs@example.com")
      with mock.patch.dict(os.environ, {"JOB_SCOUT_CHANNEL_DB": str(database)}, clear=False), \
           mock.patch.object(zimlinks, "classify_with_model") as classifier:
        candidates = zimlinks.fetch(limit=5)
      classifier.assert_not_called()
      self.assertEqual(len(candidates), 1)
      self.assertEqual(candidates[0]["source"], "zimlinks-whatsapp")
      self.assertEqual(candidates[0]["role"], "Driver")
      self.assertEqual(candidates[0]["company"], "Acme Logistics")
      self.assertEqual(candidates[0]["applicationEmail"], "jobs@example.com")
      self.assertEqual(candidates[0]["applyLinks"], [])

  def test_channel_metadata_assigns_distinct_source_and_name(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      database = self.create_db(Path(tmp))
      self.insert_post(database, "new-source", "VACANCY: Driver\nCompany: Example")
      conn = sqlite3.connect(database)
      conn.execute(
        "INSERT INTO listener_meta(key,value,updated_at) VALUES(?,?,?)",
        (
          "channel:123@newsletter",
          json.dumps({
            "jid": "123@newsletter",
            "name": "ZIM•JOB•VACANCIES🇿🇼",
            "source": "zim-job-vacancies-whatsapp",
          }),
          datetime.now(timezone.utc).isoformat(),
        ),
      )
      conn.commit()
      conn.close()
      with mock.patch.dict(os.environ, {"JOB_SCOUT_CHANNEL_DB": str(database)}, clear=False):
        candidate = zimlinks.fetch(limit=5)[0]
      self.assertEqual(candidate["source"], "zim-job-vacancies-whatsapp")
      self.assertEqual(candidate["sourceChannelName"], "ZIM•JOB•VACANCIES🇿🇼")

  def test_non_job_post_is_purged_without_model(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      database = self.create_db(Path(tmp))
      self.insert_post(database, "11", "Two rooms available to let in Harare")
      with mock.patch.dict(os.environ, {"JOB_SCOUT_CHANNEL_DB": str(database)}, clear=False), \
           mock.patch.object(zimlinks, "classify_with_model") as classifier:
        self.assertEqual(zimlinks.fetch(limit=5), [])
      classifier.assert_not_called()
      conn = sqlite3.connect(database)
      status, raw_text, purged = conn.execute("SELECT status,raw_text,raw_purged FROM posts").fetchone()
      conn.close()
      self.assertEqual((status, raw_text, purged), ("terminal", None, 1))

  def test_ambiguous_post_uses_structured_model_but_contacts_stay_deterministic(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      database = self.create_db(Path(tmp))
      self.insert_post(database, "12", "Graduate opportunity. Details: https://apply.example/jobs/12")
      result = {
        "isJob": True,
        "role": "Graduate Trainee",
        "company": "Example Ltd",
        "location": "Harare",
        "closing": None,
        "description": "Graduate trainee opportunity",
      }
      with mock.patch.dict(os.environ, {"JOB_SCOUT_CHANNEL_DB": str(database)}, clear=False), \
           mock.patch.object(zimlinks, "classify_with_model", return_value=(result, "test/model")):
        candidate = zimlinks.fetch(limit=5)[0]
      self.assertEqual(candidate["applyLinks"], ["https://apply.example/jobs/12"])
      self.assertEqual(candidate["applicationEmails"], [])
      self.assertEqual(candidate["role"], "Graduate Trainee")

  def test_model_call_does_not_block_listener_write(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      database = self.create_db(Path(tmp))
      self.insert_post(
        database,
        "ambiguous",
        "Graduate opportunity. Details: https://apply.example/jobs/ambiguous",
        posted_at="2026-01-01T00:00:00+00:00",
      )
      # This newer non-job row is processed first and performs a write before
      # process_pending reaches the ambiguous row and calls the model.
      self.insert_post(
        database,
        "non-job",
        "Two rooms available to let in Harare",
        posted_at="2026-01-02T00:00:00+00:00",
      )

      def classify_while_listener_writes(_text: str, *, timeout: int) -> tuple[dict, str]:
        self.assertEqual(timeout, 5)
        self.insert_post(
          database,
          "arrived-during-model-call",
          "VACANCY: Driver",
          posted_at="2026-01-03T00:00:00+00:00",
          timeout=0.1,
        )
        return (
          {
            "isJob": True,
            "role": "Graduate Trainee",
            "company": "Example Ltd",
            "location": "Harare",
            "closing": None,
            "description": "Graduate trainee opportunity",
          },
          "test/model",
        )

      with mock.patch.dict(os.environ, {"JOB_SCOUT_CHANNEL_DB": str(database)}, clear=False), \
           mock.patch.object(zimlinks, "classify_with_model", side_effect=classify_while_listener_writes):
        candidates = zimlinks.fetch(limit=5, timeout=5)

      self.assertEqual([candidate["sourcePostId"] for candidate in candidates], ["ambiguous"])
      conn = sqlite3.connect(database)
      statuses = dict(conn.execute("SELECT post_id,status FROM posts").fetchall())
      conn.close()
      self.assertEqual(statuses["non-job"], "terminal")
      self.assertEqual(statuses["ambiguous"], "candidate")
      self.assertEqual(statuses["arrived-during-model-call"], "pending")

  def test_raw_media_waits_for_all_selected_users_then_is_purged(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      database = self.create_db(root)
      media = root / "media.jpg"
      media.write_bytes(b"media")
      self.insert_post(database, "13", "VACANCY: Driver", media_path=str(media))
      candidate = {
        "source": "zim-job-vacancies-whatsapp",
        "sourceChannelJid": "123@newsletter",
        "sourcePostId": "13",
      }
      with mock.patch.dict(os.environ, {"JOB_SCOUT_CHANNEL_DB": str(database)}, clear=False):
        zimlinks.register_eligibility("run-1", "user-a", [candidate])
        zimlinks.register_eligibility("run-1", "user-b", [candidate])
        zimlinks.record_outcome("run-1", "user-a", candidate, "applied", 1)
        zimlinks.finalize_dispatch("run-1")
        self.assertTrue(media.exists())
        zimlinks.record_outcome("run-1", "user-b", candidate, "action_required", 0)
        zimlinks.finalize_dispatch("run-1")
      conn = sqlite3.connect(database)
      raw_text, purged = conn.execute("SELECT raw_text,raw_purged FROM posts").fetchone()
      conn.close()
      self.assertEqual((raw_text, purged), (None, 1))
      self.assertFalse(media.exists())


if __name__ == "__main__":
  unittest.main()
