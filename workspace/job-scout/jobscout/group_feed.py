"""Cached WhatsApp group feed support for Job Scout.

The existing subscriber dispatch owns discovery. This module writes a broad
cache during that run and lets a separate delivery cron post only from the cache.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re

from .constants import (
  DEFAULT_GROUP_FEED_CACHE_MAX_AGE_SECONDS,
  DEFAULT_GROUP_FEED_LIMIT,
  DEFAULT_GROUP_FEED_ROOT,
)
from .jsearch import (
  application_key,
  closing_state,
  fetch_scraped_candidates_with_results,
  fetch_whatsapp_channel_candidates_with_results,
)
from .textutil import public_text
from .util import safe_run_id, utc_now


SOURCE_NAMES = (*("zimlinks-whatsapp", "zim-job-vacancies-whatsapp", "everyday-vacancies-whatsapp"), "vacancymail", "iharare", "vacancybox", "applynow")


def _write_json(path: Path, data: dict) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  tmp = path.with_suffix(path.suffix + ".tmp")
  tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  tmp.replace(path)


def _load_json(path: Path) -> dict:
  try:
    data = json.loads(path.read_text(encoding="utf-8"))
  except FileNotFoundError as err:
    raise SystemExit(f"Job Scout group-feed cache is missing: {path}") from err
  except json.JSONDecodeError as err:
    raise SystemExit(f"Job Scout group-feed JSON is invalid: {path}") from err
  if not isinstance(data, dict):
    raise SystemExit(f"Job Scout group-feed JSON must be an object: {path}")
  return data


def group_feed_preferences() -> dict:
  return {
    "country": "zw",
    "locations": ["Nationwide, Zimbabwe"],
    "targetRoles": [],
    "roles": [],
  }


def candidate_delivery_key(candidate: dict) -> str:
  parts: list[str]
  channel_jid = str(candidate.get("sourceChannelJid") or "").strip()
  post_id = str(candidate.get("sourcePostId") or "").strip()
  source_url = str(candidate.get("sourceUrl") or candidate.get("sourcePostUrl") or "").strip()
  candidate_id = str(candidate.get("candidateId") or "").strip()
  if channel_jid and post_id:
    parts = ["channel", channel_jid, post_id]
  elif source_url:
    parts = ["url", source_url]
  elif candidate_id:
    parts = ["id", str(candidate.get("source") or ""), candidate_id]
  else:
    parts = [
      "fallback",
      str(candidate.get("company") or ""),
      str(candidate.get("role") or ""),
      str(candidate.get("location") or ""),
      str(candidate.get("description") or "")[:500],
    ]
  return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _dedupe_candidates(candidates: list[dict], limit: int) -> tuple[list[dict], dict[str, int]]:
  output: list[dict] = []
  seen_delivery_keys: set[str] = set()
  seen_company_roles: set[tuple[str, str]] = set()
  stats = {
    "eligibleCount": 0,
    "expiredCount": 0,
    "duplicateCount": 0,
    "unknownDeadlineCount": 0,
    "malformedDeadlineCount": 0,
  }
  for candidate in candidates:
    deadline = closing_state(candidate.get("closing"))
    if deadline == "expired":
      stats["expiredCount"] += 1
      continue
    if deadline == "unknown":
      stats["unknownDeadlineCount"] += 1
    elif deadline == "malformed":
      stats["malformedDeadlineCount"] += 1
    delivery_key = candidate_delivery_key(candidate)
    if delivery_key in seen_delivery_keys:
      stats["duplicateCount"] += 1
      continue
    company_role = application_key(candidate)
    if company_role != ("unknown employer", "unknown role") and company_role in seen_company_roles:
      stats["duplicateCount"] += 1
      continue
    if len(output) >= max(0, int(limit or 0)):
      continue
    enriched = {**candidate, "groupFeedKey": delivery_key}
    output.append(enriched)
    seen_delivery_keys.add(delivery_key)
    seen_company_roles.add(company_role)
    stats["eligibleCount"] += 1
  return output, stats


def fetch_group_feed_candidates(limit: int = DEFAULT_GROUP_FEED_LIMIT) -> tuple[list[dict], dict[str, dict]]:
  preferences = group_feed_preferences()
  channel_candidates, channel_results = fetch_whatsapp_channel_candidates_with_results(preferences, limit)
  board_candidates, board_results = fetch_scraped_candidates_with_results(preferences, limit)
  source_results = {**channel_results, **board_results}
  for source in SOURCE_NAMES:
    source_results.setdefault(source, {"status": "skipped", "rawCount": 0, "durationMs": 0})
  for result in source_results.values():
    if result.get("error"):
      result["error"] = public_text(result["error"], 180)
  candidates, filter_stats = _dedupe_candidates(channel_candidates + board_candidates, limit)
  for result in source_results.values():
    for key in (
      "eligibleCount",
      "expiredCount",
      "duplicateCount",
      "unknownDeadlineCount",
      "malformedDeadlineCount",
    ):
      result.setdefault(key, 0)
  for candidate in candidates:
    source = str(candidate.get("source") or "unknown")
    source_results.setdefault(source, {"status": "ok", "rawCount": 0, "durationMs": 0})
  source_results["_filter"] = filter_stats
  return candidates, source_results


def latest_cache_path(root: Path = DEFAULT_GROUP_FEED_ROOT) -> Path:
  return Path(root) / "latest.json"


def delivered_state_path(root: Path = DEFAULT_GROUP_FEED_ROOT) -> Path:
  return Path(root) / "delivered.json"


def write_group_feed_cache(
  run_id: str,
  candidates: list[dict],
  source_results: dict[str, dict],
  root: Path = DEFAULT_GROUP_FEED_ROOT,
) -> dict:
  safe_id = safe_run_id(run_id)
  data = {
    "schemaVersion": 1,
    "runId": safe_id,
    "createdAt": utc_now(),
    "candidateCount": len(candidates),
    "sourceResults": source_results,
    "candidates": candidates,
  }
  root = Path(root)
  _write_json(root / "runs" / f"{safe_id}.json", data)
  _write_json(latest_cache_path(root), data)
  return {"ok": True, "path": str(latest_cache_path(root)), "candidateCount": len(candidates)}


def build_group_feed_cache(
  run_id: str,
  root: Path = DEFAULT_GROUP_FEED_ROOT,
  limit: int = DEFAULT_GROUP_FEED_LIMIT,
  write: bool = True,
) -> dict:
  candidates, source_results = fetch_group_feed_candidates(limit)
  if not write:
    return {
      "ok": True,
      "dryRun": True,
      "runId": safe_run_id(run_id),
      "candidateCount": len(candidates),
      "sourceResults": source_results,
      "candidates": candidates,
    }
  return {
    **write_group_feed_cache(run_id, candidates, source_results, root),
    "sourceResults": source_results,
  }


def _parse_utc(value: object) -> datetime | None:
  text = str(value or "").strip()
  if not text:
    return None
  try:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
  except ValueError:
    return None
  if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=timezone.utc)
  return parsed.astimezone(timezone.utc)


def load_latest_cache(
  root: Path = DEFAULT_GROUP_FEED_ROOT,
  max_age_seconds: int = DEFAULT_GROUP_FEED_CACHE_MAX_AGE_SECONDS,
) -> dict:
  data = _load_json(latest_cache_path(root))
  created_at = _parse_utc(data.get("createdAt"))
  if created_at is None:
    raise SystemExit("Job Scout group-feed cache is missing a valid createdAt timestamp")
  age = (datetime.now(timezone.utc) - created_at).total_seconds()
  if max_age_seconds >= 0 and age > max_age_seconds:
    raise SystemExit(
      f"Job Scout group-feed cache is stale: {int(age)}s old, max {int(max_age_seconds)}s"
    )
  candidates = data.get("candidates")
  if not isinstance(candidates, list):
    raise SystemExit("Job Scout group-feed cache has no candidate list")
  return data


def load_delivered_state(root: Path = DEFAULT_GROUP_FEED_ROOT) -> dict:
  path = delivered_state_path(root)
  if not path.exists():
    return {"schemaVersion": 1, "updatedAt": None, "delivered": {}}
  data = _load_json(path)
  delivered = data.get("delivered")
  if not isinstance(delivered, dict):
    raise SystemExit("Job Scout group-feed delivered state has no delivered object")
  return data


def save_delivered_state(root: Path, state: dict) -> None:
  state["schemaVersion"] = 1
  state["updatedAt"] = utc_now()
  if not isinstance(state.get("delivered"), dict):
    state["delivered"] = {}
  _write_json(delivered_state_path(root), state)


def delivery_lock_path(root: Path = DEFAULT_GROUP_FEED_ROOT) -> Path:
  return Path(root) / "delivery.lock"


@contextmanager
def exclusive_delivery_lock(root: Path = DEFAULT_GROUP_FEED_ROOT):
  path = delivery_lock_path(root)
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open("a+", encoding="utf-8") as lock_file:
    try:
      fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as err:
      raise SystemExit("another Job Scout group-feed delivery is already running") from err
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(json.dumps({"pid": os.getpid(), "startedAt": utc_now()}) + "\n")
    lock_file.flush()
    try:
      yield
    finally:
      fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _clean_line(value: object, limit: int = 400) -> str:
  text = re.sub(r"\s+", " ", str(value or "")).strip()
  return text[:limit].rstrip()


SECTION_HEADINGS = (
  "About Us",
  "Role Overview",
  "Job Summary",
  "Duties and Responsibilities",
  "Key Responsibilities",
  "Responsibilities",
  "Qualifications and Experience",
  "Qualifications & Experience",
  "Requirements",
  "Skills",
  "Company Benefits",
  "Benefits",
  "How to Apply",
  "Application",
  "Closing Date",
)


def _description_text(value: object) -> str:
  text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
  if not text:
    return ""
  if "\n" not in text:
    text = re.sub(r"\s*[\u2022]\s*", "\n- ", text)
    heading_pattern = "|".join(re.escape(heading) for heading in sorted(SECTION_HEADINGS, key=len, reverse=True))
    text = re.sub(
      rf"\s+({heading_pattern})(?:\s*:)?\s+",
      r"\n\n\1\n\n",
      text,
      flags=re.I,
    )
  lines: list[str] = []
  blank = False
  for raw in text.splitlines():
    line = re.sub(r"[ \t]+", " ", raw).strip()
    if not line:
      if lines and not blank:
        lines.append("")
      blank = True
      continue
    lines.append(line)
    blank = False
  return "\n".join(lines).strip()


def _description_has(description: str, value: object) -> bool:
  text = str(value or "").strip()
  return bool(text) and text.casefold() in description.casefold()


def format_group_feed_message(candidate: dict) -> str:
  role = _clean_line(candidate.get("role"), 180) or "Unknown role"
  location = _clean_line(candidate.get("location"), 180)
  company = _clean_line(candidate.get("company"), 180)
  description = _description_text(candidate.get("description"))
  lines = [f"Job Title: {role}", ""]
  if location:
    lines.extend([f"Location: {location}", ""])
  if company and company.casefold() != "unknown employer" and not _description_has(description, company):
    lines.extend([f"Company: {company}", ""])
  if description:
    lines.extend([description, ""])

  application_lines: list[str] = []
  closing = _clean_line(candidate.get("closing"), 120)
  if closing and not _description_has(description, closing):
    application_lines.append(f"Closing Date: {closing}")
  emails = [str(item).strip() for item in candidate.get("applicationEmails") or [] if str(item).strip()]
  if candidate.get("applicationEmail") and str(candidate["applicationEmail"]).strip() not in emails:
    emails.insert(0, str(candidate["applicationEmail"]).strip())
  for email in emails:
    if not _description_has(description, email):
      application_lines.append(f"Email: {email}")
  links: list[str] = []
  for key in ("applyLinks", "contactLinks"):
    values = candidate.get(key)
    if isinstance(values, list):
      for value in values:
        text = str(value or "").strip()
        if text and text not in links:
          links.append(text)
  source = str(candidate.get("sourcePostUrl") or candidate.get("sourceUrl") or "").strip()
  for link in links:
    if link != source and not _description_has(description, link):
      application_lines.append(f"Apply: {link}")
  if source and not _description_has(description, source):
    application_lines.append(f"Source: {source}")
  if application_lines:
    lines.extend(["Application", "", *application_lines])
  return "\n".join(lines).strip()


def delivery_record(candidate: dict, result: dict, target: str, run_id: str) -> dict:
  message_id = None
  for key in ("messageId", "id"):
    value = str(result.get(key) or "").strip()
    if value:
      message_id = value
      break
  return {
    "deliveredAt": utc_now(),
    "target": target,
    "runId": safe_run_id(run_id),
    "messageId": message_id,
    "source": str(candidate.get("source") or ""),
    "sourceUrl": str(candidate.get("sourcePostUrl") or candidate.get("sourceUrl") or ""),
    "company": _clean_line(candidate.get("company"), 180),
    "role": _clean_line(candidate.get("role"), 180),
  }
