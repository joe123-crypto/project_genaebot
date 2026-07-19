#!/usr/bin/env python3
"""Bounded classification/normalization of untrusted WhatsApp channel posts.

Pure text handling plus the structured-model fallback: turn a raw WhatsApp
Channel post into a Job Scout candidate dict (per-channel source/name comes
from the listener_meta channel record). No SQLite or dispatch state — the
persistence/lifecycle layer lives in zimlinks.py and calls into this module.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request

from . import base
from jobscout.constants import CANDIDATE_DESCRIPTION_CHAR_LIMIT


SOURCE = "zimlinks-whatsapp"
DEFAULT_MODEL = "deepseek/deepseek-v4-pro"
AI_GATEWAY_URL = "https://ai-gateway.vercel.sh/v1/chat/completions"
RAW_RETENTION_DAYS = 14

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


def candidate_from(row: sqlite3.Row, fields: dict, channel: dict | None = None) -> dict:
  text = str(row["raw_text"] or "")
  apply_links, contact_links = original_urls(text)
  role = clean_line(fields.get("role"), 120) or "Unknown role"
  company = clean_line(fields.get("company"), 120) or "Unknown employer"
  description = (
    clean_line(fields.get("description"), CANDIDATE_DESCRIPTION_CHAR_LIMIT)
    or base.collapse_ws(text)[:CANDIDATE_DESCRIPTION_CHAR_LIMIT]
  )
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
