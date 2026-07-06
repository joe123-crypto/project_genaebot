#!/usr/bin/env python3
"""Transient WhatsApp Channel sources for Job Scout job adverts.

The isolated Node listener captures raw posts into SQLite. This module performs
bounded classification/normalization and exposes candidates in Job Scout's
existing shape. Raw payloads are deliberately disposable; central Job Scout
application records remain the durable audit trail.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3

from . import base
from . import post_classifier


DEFAULT_DB = Path("/home/joseph/.openclaw/runtime/job-scout-channel/posts.sqlite3")
MAX_MODEL_POSTS_PER_FETCH = 10
MAX_PARSE_ATTEMPTS = 3
TOMBSTONE_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  channel_jid TEXT NOT NULL,
  post_id TEXT NOT NULL,
  posted_at TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  post_url TEXT NOT NULL,
  raw_text TEXT,
  media_path TEXT,
  media_mime TEXT,
  media_sha256 TEXT,
  media_bytes INTEGER,
  status TEXT NOT NULL DEFAULT 'pending',
  candidate_json TEXT,
  expires_at TEXT,
  parse_attempts INTEGER NOT NULL DEFAULT 0,
  parse_error TEXT,
  model_used TEXT,
  raw_purged INTEGER NOT NULL DEFAULT 0,
  terminal_at TEXT,
  tombstone_until TEXT,
  PRIMARY KEY (channel_jid, post_id)
);
CREATE TABLE IF NOT EXISTS post_eligibility (
  run_id TEXT NOT NULL, channel_jid TEXT NOT NULL, post_id TEXT NOT NULL,
  user_key TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, channel_jid, post_id, user_key)
);
CREATE TABLE IF NOT EXISTS post_outcomes (
  run_id TEXT NOT NULL, channel_jid TEXT NOT NULL, post_id TEXT NOT NULL,
  user_key TEXT NOT NULL, outcome TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, channel_jid, post_id, user_key)
);
CREATE TABLE IF NOT EXISTS listener_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


def db_path() -> Path:
  return Path(os.environ.get("JOB_SCOUT_CHANNEL_DB", str(DEFAULT_DB))).expanduser()


def connect(path: Path | None = None) -> sqlite3.Connection | None:
  target = path or db_path()
  if not target.exists():
    return None
  conn = sqlite3.connect(target, timeout=5)
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA busy_timeout=5000")
  conn.executescript(SCHEMA)
  return conn


def utc_now() -> str:
  return datetime.now(timezone.utc).isoformat()


def channel_metadata(conn: sqlite3.Connection) -> dict[str, dict]:
  metadata: dict[str, dict] = {}
  try:
    rows = conn.execute(
      "SELECT key,value FROM listener_meta WHERE key='channel' OR key LIKE 'channel:%' ORDER BY key",
    ).fetchall()
  except sqlite3.Error:
    return metadata
  for row in rows:
    try:
      value = json.loads(row["value"])
    except (TypeError, json.JSONDecodeError):
      continue
    if not isinstance(value, dict):
      continue
    jid = str(value.get("jid") or "").strip()
    if jid:
      metadata[jid] = value
  return metadata


def safe_unlink(media_path: str | None, database: Path) -> None:
  if not media_path:
    return
  target = Path(media_path)
  allowed = database.parent.resolve()
  try:
    resolved = target.resolve()
    if resolved != allowed and allowed not in resolved.parents:
      return
    resolved.unlink(missing_ok=True)
  except OSError:
    pass


def purge_raw(conn: sqlite3.Connection, row: sqlite3.Row, *, terminal: bool, reason: str | None = None) -> None:
  safe_unlink(row["media_path"], db_path())
  now = datetime.now(timezone.utc)
  if terminal:
    conn.execute(
      """
      UPDATE posts SET status='terminal', raw_text=NULL, media_path=NULL, media_mime=NULL,
        media_sha256=NULL, media_bytes=NULL, raw_purged=1, terminal_at=?, tombstone_until=?, parse_error=?
      WHERE channel_jid=? AND post_id=?
      """,
      (now.isoformat(), (now + timedelta(days=TOMBSTONE_DAYS)).isoformat(), reason, row["channel_jid"], row["post_id"]),
    )
  else:
    conn.execute(
      """
      UPDATE posts SET raw_text=NULL, media_path=NULL, media_mime=NULL,
        media_sha256=NULL, media_bytes=NULL, raw_purged=1
      WHERE channel_jid=? AND post_id=?
      """,
      (row["channel_jid"], row["post_id"]),
    )


def save_candidate(conn: sqlite3.Connection, row: sqlite3.Row, candidate: dict, model: str | None) -> None:
  conn.execute(
    """
    UPDATE posts SET status='candidate', candidate_json=?, expires_at=?,
      parse_attempts=parse_attempts+1, parse_error=NULL, model_used=?
    WHERE channel_jid=? AND post_id=?
    """,
    (
      json.dumps(candidate, separators=(",", ":"), sort_keys=True),
      post_classifier.post_expiry(row["posted_at"], candidate.get("closing")),
      model,
      row["channel_jid"], row["post_id"],
    ),
  )


def process_pending(conn: sqlite3.Connection, timeout: int) -> None:
  rows = conn.execute(
    """
    SELECT * FROM posts
    WHERE status IN ('pending','retry') AND raw_text IS NOT NULL AND parse_attempts < ?
    ORDER BY posted_at DESC, first_seen_at DESC
    """,
    (MAX_PARSE_ATTEMPTS,),
  ).fetchall()
  channels = channel_metadata(conn)
  model_calls = 0
  for row in rows:
    text = str(row["raw_text"] or "").strip()
    if not text:
      with conn:
        purge_raw(conn, row, terminal=True, reason="empty_text")
      continue
    verdict, fields = post_classifier.deterministic_verdict(text)
    if verdict == "not_job":
      with conn:
        purge_raw(conn, row, terminal=True, reason="not_job")
      continue
    model = None
    if verdict == "ambiguous":
      if model_calls >= MAX_MODEL_POSTS_PER_FETCH:
        continue
      model_calls += 1
      # Never hold SQLite's single-writer lock while waiting on the network.
      # This is defensive even though every mutation above commits immediately:
      # future callers or processing branches may already have opened a transaction.
      conn.commit()
      try:
        result, model = post_classifier.classify_with_model(text, timeout=timeout)
      except Exception as err:
        attempts = int(row["parse_attempts"] or 0) + 1
        with conn:
          if attempts >= MAX_PARSE_ATTEMPTS:
            purge_raw(conn, row, terminal=True, reason=f"classifier_failed: {str(err)[:300]}")
          else:
            conn.execute(
              "UPDATE posts SET status='retry', parse_attempts=?, parse_error=? WHERE channel_jid=? AND post_id=?",
              (attempts, str(err)[:500], row["channel_jid"], row["post_id"]),
            )
        continue
      if not result.get("isJob"):
        with conn:
          purge_raw(conn, row, terminal=True, reason="model_not_job")
        continue
      fields = {name: result.get(name) or fields.get(name, "") for name in ("role", "company", "location", "closing", "description")}
    else:
      fields["description"] = text
    with conn:
      save_candidate(conn, row, post_classifier.candidate_from(row, fields, channels.get(str(row["channel_jid"]))), model)


def cleanup(conn: sqlite3.Connection) -> None:
  now = utc_now()
  expired = conn.execute(
    "SELECT * FROM posts WHERE status='candidate' AND expires_at IS NOT NULL AND expires_at <= ?",
    (now,),
  ).fetchall()
  for row in expired:
    purge_raw(conn, row, terminal=True, reason="expired")
  conn.execute("DELETE FROM posts WHERE status='terminal' AND tombstone_until IS NOT NULL AND tombstone_until <= ?", (now,))
  conn.execute("DELETE FROM post_eligibility WHERE created_at < datetime('now','-30 days')")
  conn.execute("DELETE FROM post_outcomes WHERE updated_at < datetime('now','-30 days')")
  conn.commit()


def fetch(limit: int = 20, timeout: int = base.DEFAULT_TIMEOUT, match=None) -> list[dict]:
  conn = connect()
  if conn is None:
    return []
  try:
    cleanup(conn)
    process_pending(conn, timeout)
    rows = conn.execute(
      "SELECT candidate_json FROM posts WHERE status='candidate' AND candidate_json IS NOT NULL ORDER BY posted_at DESC, first_seen_at DESC",
    ).fetchall()
    candidates: list[dict] = []
    for row in rows:
      try:
        candidate = json.loads(row["candidate_json"])
      except (TypeError, json.JSONDecodeError):
        continue
      if match is not None and not match(str(candidate.get("role") or ""), str(candidate.get("description") or "")):
        continue
      candidates.append(candidate)
      if len(candidates) >= max(0, limit):
        break
    return candidates
  finally:
    conn.close()


def register_eligibility(run_id: str, user_key: str, candidates: list[dict]) -> None:
  conn = connect()
  if conn is None:
    return
  try:
    now = utc_now()
    for candidate in candidates:
      if not candidate.get("sourceChannelJid") or not candidate.get("sourcePostId"):
        continue
      conn.execute(
        "INSERT OR IGNORE INTO post_eligibility(run_id,channel_jid,post_id,user_key,created_at) VALUES (?,?,?,?,?)",
        (run_id, candidate.get("sourceChannelJid"), candidate.get("sourcePostId"), user_key, now),
      )
    conn.commit()
  finally:
    conn.close()


def record_outcome(run_id: str, user_key: str, candidate: dict, outcome: str, attempt_count: int = 0) -> None:
  if not candidate.get("sourceChannelJid") or not candidate.get("sourcePostId"):
    return
  conn = connect()
  if conn is None:
    return
  try:
    conn.execute(
      """
      INSERT INTO post_outcomes(run_id,channel_jid,post_id,user_key,outcome,attempt_count,updated_at)
      VALUES (?,?,?,?,?,?,?)
      ON CONFLICT(run_id,channel_jid,post_id,user_key) DO UPDATE SET
        outcome=excluded.outcome, attempt_count=excluded.attempt_count, updated_at=excluded.updated_at
      """,
      (
        run_id, candidate.get("sourceChannelJid"), candidate.get("sourcePostId"), user_key,
        outcome, max(0, int(attempt_count or 0)), utc_now(),
      ),
    )
    conn.commit()
  finally:
    conn.close()


def finalize_dispatch(run_id: str) -> None:
  conn = connect()
  if conn is None:
    return
  try:
    posts = conn.execute(
      "SELECT DISTINCT channel_jid, post_id FROM post_eligibility WHERE run_id=?",
      (run_id,),
    ).fetchall()
    terminal = {"applied", "action_required", "skipped"}
    finalized: list[tuple[str, str]] = []
    for ref in posts:
      eligibility = conn.execute(
        "SELECT user_key FROM post_eligibility WHERE run_id=? AND channel_jid=? AND post_id=?",
        (run_id, ref["channel_jid"], ref["post_id"]),
      ).fetchall()
      outcomes = conn.execute(
        "SELECT user_key,outcome,attempt_count FROM post_outcomes WHERE run_id=? AND channel_jid=? AND post_id=?",
        (run_id, ref["channel_jid"], ref["post_id"]),
      ).fetchall()
      by_user = {row["user_key"]: row for row in outcomes}
      if any(row["user_key"] not in by_user for row in eligibility):
        continue
      if any(row["outcome"] not in terminal and not (row["outcome"] == "failed" and int(row["attempt_count"]) >= MAX_PARSE_ATTEMPTS) for row in outcomes):
        continue
      post = conn.execute(
        "SELECT * FROM posts WHERE channel_jid=? AND post_id=?",
        (ref["channel_jid"], ref["post_id"]),
      ).fetchone()
      if post:
        purge_raw(conn, post, terminal=False)
      finalized.append((ref["channel_jid"], ref["post_id"]))
    for channel_jid, post_id in finalized:
      conn.execute(
        "DELETE FROM post_eligibility WHERE run_id=? AND channel_jid=? AND post_id=?",
        (run_id, channel_jid, post_id),
      )
      conn.execute(
        "DELETE FROM post_outcomes WHERE run_id=? AND channel_jid=? AND post_id=?",
        (run_id, channel_jid, post_id),
      )
    conn.commit()
  finally:
    conn.close()
