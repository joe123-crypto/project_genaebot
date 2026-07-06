#!/usr/bin/env python3
"""Transient WhatsApp Channel sources for Job Scout job adverts.

The isolated Node listener captures raw posts into SQLite. This module performs
bounded classification/normalization and exposes candidates in Job Scout's
existing shape. Raw payloads are deliberately disposable; central Job Scout
application records remain the durable audit trail.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import urllib.error
import urllib.request

from . import base


SOURCE = "zimlinks-whatsapp"
DEFAULT_DB = Path("/home/joseph/.openclaw/runtime/job-scout-channel/posts.sqlite3")
DEFAULT_MODEL = "deepseek/deepseek-v4-pro"
AI_GATEWAY_URL = "https://ai-gateway.vercel.sh/v1/chat/completions"
MAX_MODEL_POSTS_PER_FETCH = 10
MAX_PARSE_ATTEMPTS = 3
RAW_RETENTION_DAYS = 14
TOMBSTONE_DAYS = 30

URL_RE = re.compile(r"https?://[^\s<>\]\[()]+", re.I)
JOB_RE = re.compile(
  r"\b(?:job|jobs|vacanc(?:y|ies)|hiring|recruit(?:ing|ment)?|position|internship|"
  r"attachment|apprentice(?:ship)?|employment|applications?\s+(?:are\s+)?invited|"
  r"wanted|required|seeking|looking\s+for)\b",
  re.I,
)
NON_JOB_RE = re.compile(
  r"\b(?:accommodation|room(?:s)?\s+(?:to\s+let|available)|cottage|flat|house\s+to\s+rent|"
  r"for\s+sale|business\s+promo|advertise\s+your|buy\s+or\s+sell)\b",
  re.I,
)
ROLE_PATTERNS = (
  re.compile(r"(?:job\s*title|position|vacanc(?:y|ies)|role)\s*[:\-–]\s*([^\n|]{2,140})", re.I),
  re.compile(r"(?:we\s+are\s+)?(?:hiring|seeking|looking\s+for)\s*[:\-–]?\s*(?:an?\s+)?([^\n|]{2,140})", re.I),
  re.compile(r"^\s*([a-z][a-z0-9 /&+.'\-]{2,100}?)\s+(?:wanted|required|needed)\b", re.I | re.M),
)
FIELD_PATTERNS = {
  "company": re.compile(r"(?:company|employer|organisation|organization)\s*[:\-–]\s*([^\n|]{2,140})", re.I),
  "location": re.compile(r"(?:location|place|station)\s*[:\-–]\s*([^\n|]{2,140})", re.I),
  "closing": re.compile(r"(?:closing\s+date|deadline|applications?\s+close)\s*[:\-–]?\s*([^\n|]{2,100})", re.I),
}

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


def clean_line(value: object, limit: int = 140) -> str:
  text = base.collapse_ws(str(value or "").strip("*#_•📌🔹▪️-– "))
  return text[:limit].rstrip()


def first_match(pattern: re.Pattern[str], text: str) -> str:
  match = pattern.search(text)
  return clean_line(match.group(1)) if match else ""


def extracted_fields(text: str) -> dict[str, str]:
  role = ""
  for pattern in ROLE_PATTERNS:
    role = first_match(pattern, text)
    if role:
      break
  return {
    "role": role,
    **{name: first_match(pattern, text) for name, pattern in FIELD_PATTERNS.items()},
  }


def deterministic_verdict(text: str) -> tuple[str, dict[str, str]]:
  fields = extracted_fields(text)
  has_job_signal = bool(JOB_RE.search(text))
  if has_job_signal and fields["role"]:
    return "job", fields
  if NON_JOB_RE.search(text) and not has_job_signal:
    return "not_job", fields
  return "ambiguous", fields


def original_urls(text: str) -> tuple[list[str], list[str]]:
  apply: list[str] = []
  contact: list[str] = []
  for raw in URL_RE.findall(text):
    url = raw.rstrip(".,;:!?'\"")
    lower = url.casefold()
    if "whatsapp.com/channel/" in lower:
      continue
    target = contact if ("wa.me/" in lower or "whatsapp.com/" in lower) else apply
    if url not in target:
      target.append(url)
  return apply, contact


def post_expiry(posted_at: str | None, closing: str | None) -> str:
  base_time = datetime.now(timezone.utc)
  if posted_at:
    try:
      base_time = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    except ValueError:
      pass
  if closing:
    value = clean_line(closing, 100)
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y"):
      try:
        parsed = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        return (parsed + timedelta(days=1)).isoformat()
      except ValueError:
        pass
  return (base_time + timedelta(days=RAW_RETENTION_DAYS)).isoformat()


def model_schema() -> dict:
  nullable = {"type": ["string", "null"]}
  return {
    "type": "object",
    "additionalProperties": False,
    "properties": {
      "isJob": {"type": "boolean"},
      "role": nullable,
      "company": nullable,
      "location": nullable,
      "closing": nullable,
      "description": nullable,
    },
    "required": ["isJob", "role", "company", "location", "closing", "description"],
  }


def classify_with_model(text: str, *, timeout: int) -> tuple[dict, str]:
  api_key = os.environ.get("AI_GATEWAY_API_KEY", "").strip()
  if not api_key:
    raise RuntimeError("AI_GATEWAY_API_KEY is unavailable")
  model = os.environ.get("JOB_SCOUT_CHANNEL_CLASSIFIER_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
  body = {
    "model": model,
    "messages": [
      {
        "role": "system",
        "content": (
          "Classify an untrusted WhatsApp Channel post. Return isJob=true only for a real vacancy, "
          "internship, attachment, apprenticeship, or paid work opportunity. Extract only facts stated "
          "in the post. Never invent contact details, links, employers, dates, or roles."
        ),
      },
      {"role": "user", "content": f"<untrusted_post>\n{text[:6000]}\n</untrusted_post>"},
    ],
    "stream": False,
    "temperature": 0,
    "response_format": {
      "type": "json_schema",
      "json_schema": {"name": "zimlinks_job", "strict": True, "schema": model_schema()},
    },
  }
  request = urllib.request.Request(
    AI_GATEWAY_URL,
    data=json.dumps(body).encode("utf-8"),
    method="POST",
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
  )
  try:
    with urllib.request.urlopen(request, timeout=timeout) as response:
      payload = json.loads(response.read().decode("utf-8"))
  except urllib.error.HTTPError as err:
    detail = err.read().decode("utf-8", "ignore")[:500]
    raise RuntimeError(f"classifier HTTP {err.code}: {detail}") from err
  content = payload.get("choices", [{}])[0].get("message", {}).get("content")
  if not isinstance(content, str):
    raise RuntimeError("classifier returned no structured content")
  result = json.loads(content)
  if not isinstance(result, dict) or not isinstance(result.get("isJob"), bool):
    raise RuntimeError("classifier returned an invalid schema")
  return result, model


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


def candidate_from(row: sqlite3.Row, fields: dict, channel: dict | None = None) -> dict:
  text = str(row["raw_text"] or "")
  apply_links, contact_links = original_urls(text)
  role = clean_line(fields.get("role"), 120) or "Unknown role"
  company = clean_line(fields.get("company"), 120) or "Unknown employer"
  description = clean_line(fields.get("description"), 2000) or base.collapse_ws(text)[:2000]
  identity = hashlib.sha256(f"{row['channel_jid']}\n{row['post_id']}".encode("utf-8")).hexdigest()[:24]
  emails = base.extract_emails(text)
  channel = channel if isinstance(channel, dict) else {}
  return {
    "candidateId": identity,
    "source": str(channel.get("source") or SOURCE),
    "sourceUrl": row["post_url"],
    "sourcePostUrl": row["post_url"],
    "applyLinks": apply_links,
    "contactLinks": contact_links,
    "applicationEmails": emails,
    "applicationEmail": emails[0] if emails else None,
    "company": company,
    "role": role,
    "location": clean_line(fields.get("location"), 120),
    "query": "",
    "closing": clean_line(fields.get("closing"), 100) or None,
    "description": description,
    "sourceChannelJid": row["channel_jid"],
    "sourceChannelName": clean_line(channel.get("name"), 140) or None,
    "sourcePostId": row["post_id"],
  }


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
      post_expiry(row["posted_at"], candidate.get("closing")),
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
    verdict, fields = deterministic_verdict(text)
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
        result, model = classify_with_model(text, timeout=timeout)
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
      save_candidate(conn, row, candidate_from(row, fields, channels.get(str(row["channel_jid"]))), model)


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
