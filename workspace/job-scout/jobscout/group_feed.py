"""Cached WhatsApp group feed support for Job Scout.

The existing subscriber dispatch owns discovery. This module writes a broad
cache during that run and lets a separate delivery cron post only from the cache.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import re

from .constants import (
  DEFAULT_GROUP_FEED_CACHE_MAX_AGE_SECONDS,
  DEFAULT_GROUP_FEED_INVITE_URL,
  DEFAULT_GROUP_FEED_LIMIT,
  DEFAULT_GROUP_FEED_ROOT,
)
from .jsearch import (
  application_key,
  closing_state,
  fetch_scraped_candidates_with_results,
  fetch_whatsapp_channel_candidates_with_results,
  is_low_information_candidate,
)
from .textutil import display_job_title, public_text
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
    "lowInfoCount": 0,
    "missingTitleCount": 0,
    "unknownDeadlineCount": 0,
    "malformedDeadlineCount": 0,
  }
  for candidate in candidates:
    if is_low_information_candidate(candidate):
      stats["lowInfoCount"] += 1
      continue
    if not display_job_title(candidate):
      stats["missingTitleCount"] += 1
      continue
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
      "lowInfoCount",
      "missingTitleCount",
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
  text = _remove_inline_internal_fragments(_strip_markup(str(value or "")))
  text = re.sub(r"\s+", " ", text).strip()
  if _looks_internal_line(text):
    return ""
  return text[:limit].rstrip()


def _strip_markup(value: str) -> str:
  text = html.unescape(value or "")
  text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
  text = re.sub(r"<\s*(script|style|noscript|iframe|ins)\b[^>]*>.*?</\s*\1\s*>", " ", text, flags=re.I | re.S)
  text = re.sub(r"<\s*br\s*/?>", "\n", text, flags=re.I)
  text = re.sub(r"</\s*(?:p|div|li|ul|ol|h[1-6]|section|article|tr|table)\s*>", "\n", text, flags=re.I)
  text = re.sub(r"<[^>]+>", " ", text)
  return text


def _remove_inline_internal_fragments(value: str) -> str:
  text = value
  text = re.sub(
    r"\(?\s*adsbygoogle\s*=\s*window\.adsbygoogle\s*\|\|\s*\[\]\s*\)?\s*\.push\s*\([^)]*\)\s*;?",
    " ",
    text,
    flags=re.I,
  )
  text = re.sub(r"\(?\s*window\.adsbygoogle\s*=\s*window\.adsbygoogle\s*\|\|\s*\[\]\s*\)?\s*;?", " ", text, flags=re.I)
  text = re.sub(r"\bgoogletag\.cmd\.push\s*\([^)]*\)\s*;?", " ", text, flags=re.I)
  return text


INTERNAL_LINE_PATTERNS = tuple(
  re.compile(pattern, re.I)
  for pattern in (
    r"\badsbygoogle\b",
    r"\bgooglesyndication\b",
    r"\bgoogletag\b",
    r"\bgoogle_ad_",
    r"\bdoubleclick\b",
    r"\bdata-ad-",
    r"\bapplication/ld\+json\b",
    r"\bschema\.org\b",
    r'"@context"\s*:',
    r'"@type"\s*:',
    r"\bwindow\.",
    r"\bdocument\.",
    r"\bconsole\.",
    r"^\s*(?:async\s+)?function(?:\s+[A-Za-z_$][\w$]*)?\s*\([^)]*\)\s*\{",
    r"\b(?:var|let|const)\s+[A-Za-z_$][\w$]*\s*=",
    r"\bwp-(?:content|json|includes)\b",
    r"\b(?:yoast|rankmath|recaptcha|captcha)\b",
  )
)


def _looks_internal_line(line: str) -> bool:
  text = line.strip()
  if not text:
    return False
  if any(pattern.search(text) for pattern in INTERNAL_LINE_PATTERNS):
    return True
  if re.fullmatch(r"[{}\[\],;:]+", text):
    return True
  if re.search(r"[.#]?[A-Za-z0-9_-]+\s*\{[^{}]*:[^{}]*\}", text):
    return True
  if re.match(r"^[\"']?[A-Za-z0-9_.$-]+[\"']?\s*:\s*[\[{]", text):
    return True
  brace_count = text.count("{") + text.count("}") + text.count("[") + text.count("]")
  if brace_count >= 2 and (";" in text or re.search(r"[\"']\w+[\"']\s*:", text)):
    return True
  return False


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


MONTH_DATE_RE = re.compile(r"\b([0-9]{1,2}\s+[A-Za-z]+,?\s+[0-9]{4})\b")
ISO_DATE_RE = re.compile(r"\b([0-9]{4}-[0-9]{2}-[0-9]{2})\b")
SKILLS_SUBSECTION_HEADINGS = {"technology", "communication"}


def _job_title(candidate: dict) -> str:
  return display_job_title(candidate) or "Job opportunity"


def _description_text(value: object) -> str:
  text = _strip_markup(str(value or "")).replace("\r\n", "\n").replace("\r", "\n").strip()
  text = _remove_inline_internal_fragments(text)
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
    line = _remove_inline_internal_fragments(raw)
    line = re.sub(r"[ \t]+", " ", line).strip()
    if _looks_internal_line(line):
      continue
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


def _heading_key(line: str) -> str:
  return re.sub(r"\s+", " ", line.strip().rstrip(":")).casefold()


def _clean_compare_text(value: object) -> str:
  return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _prefixed_line_value(line: str, label: str) -> str | None:
  match = re.match(rf"^{re.escape(label)}\s*:\s*(.+)$", line, flags=re.I)
  if not match:
    return None
  return match.group(1).strip()


def _date_keys(value: object) -> set[str]:
  text = str(value or "")
  keys: set[str] = set()
  for pattern in (ISO_DATE_RE, MONTH_DATE_RE):
    for match in pattern.finditer(text):
      raw = match.group(1).replace(",", "")
      for fmt in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y"):
        try:
          keys.add(datetime.strptime(raw, fmt).date().isoformat())
          break
        except ValueError:
          continue
  return keys


def _next_nonblank_index(lines: list[str], start: int) -> int | None:
  for index in range(start, len(lines)):
    if lines[index].strip():
      return index
  return None


def _last_nonblank_line(lines: list[str]) -> str:
  for line in reversed(lines):
    if line.strip():
      return line.strip()
  return ""


def _remove_last_nonblank_line(lines: list[str]) -> None:
  for index in range(len(lines) - 1, -1, -1):
    if lines[index].strip():
      del lines[index]
      return


def _append_description_line(lines: list[str], line: str) -> None:
  if not line:
    if lines and lines[-1] != "":
      lines.append("")
    return
  lines.append(line)


def _compact_description_lines(lines: list[str]) -> str:
  compact: list[str] = []
  for line in lines:
    stripped = line.strip()
    if not stripped:
      if compact and compact[-1] != "":
        compact.append("")
      continue
    compact.append(stripped)
  while compact and compact[-1] == "":
    compact.pop()
  return "\n".join(compact).strip()


def _is_closing_date_heading(line: str) -> bool:
  return bool(re.match(r"^closing\s+date\b", line.strip(), flags=re.I))


def _closing_date_keys_for_line(lines: list[str], index: int) -> set[str]:
  line = lines[index].strip()
  keys = _date_keys(line)
  if keys or ":" in line:
    return keys
  next_index = _next_nonblank_index(lines, index + 1)
  if next_index is None:
    return set()
  return _date_keys(lines[next_index])


def _skip_closing_date_value(lines: list[str], index: int, keys: set[str]) -> int:
  if ":" in lines[index]:
    return index + 1
  next_index = _next_nonblank_index(lines, index + 1)
  if next_index is None:
    return index + 1
  next_keys = _date_keys(lines[next_index])
  if keys and keys & next_keys:
    return next_index + 1
  return index + 1


def _next_nonblank_is_closing_date(lines: list[str], start: int) -> bool:
  index = _next_nonblank_index(lines, start)
  return index is not None and _is_closing_date_heading(lines[index])


def _trailing_fragmented_skills_heading(lines: list[str], index: int) -> tuple[str, str] | None:
  next_index = _next_nonblank_index(lines, index + 1)
  if next_index is None or _heading_key(lines[next_index]) != "skills":
    return None
  match = re.match(r"^(.+?)\s+(competencies and|technology|communication)$", lines[index].strip(), flags=re.I)
  if not match:
    return None
  return match.group(1).strip(), match.group(2).casefold()


def _description_has_closing_date(description: str, value: object) -> bool:
  keys = _date_keys(value)
  return bool(keys and keys & _date_keys(description))


def _normalize_group_feed_description(
  description: str,
  *,
  role: str,
  location: str,
  company: str,
) -> str:
  if not description:
    return ""
  lines = description.splitlines()
  output: list[str] = []
  seen_skills = False
  seen_closing_date = False
  seen_closing_keys: set[str] = set()
  index = 0
  while index < len(lines):
    line = re.sub(r"[ \t]+", " ", lines[index]).strip()
    if not line:
      _append_description_line(output, "")
      index += 1
      continue

    title_value = _prefixed_line_value(line, "Job Title")
    location_value = _prefixed_line_value(line, "Location")
    company_value = _prefixed_line_value(line, "Company")
    if (
      title_value is not None and _clean_compare_text(title_value) == _clean_compare_text(role)
      or location_value is not None and _clean_compare_text(location_value) == _clean_compare_text(location)
      or company_value is not None and _clean_compare_text(company_value) == _clean_compare_text(company)
    ):
      index += 1
      continue

    trailing_skills_heading = _trailing_fragmented_skills_heading(lines, index)
    if trailing_skills_heading is not None:
      prefix, heading = trailing_skills_heading
      if prefix:
        _append_description_line(output, prefix)
      if heading in SKILLS_SUBSECTION_HEADINGS:
        if not seen_skills:
          _append_description_line(output, "")
          _append_description_line(output, "Skills")
          seen_skills = True
        _append_description_line(output, "")
        _append_description_line(output, heading.title())
      index += 1
      continue

    key = _heading_key(line)
    if key == "application" and seen_closing_date and _next_nonblank_is_closing_date(lines, index + 1):
      index += 1
      continue

    if key in {"competencies and", "competencies and skills"}:
      if not seen_skills:
        next_index = _next_nonblank_index(lines, index + 1)
        if next_index is not None and _heading_key(lines[next_index]) == "skills":
          index += 1
          continue
        _append_description_line(output, "Skills")
        seen_skills = True
      index += 1
      continue

    subsection_match = re.match(r"^(technology|communication)\s+skills$", key)
    if subsection_match:
      if not seen_skills:
        _append_description_line(output, "Skills")
        _append_description_line(output, "")
        seen_skills = True
      _append_description_line(output, subsection_match.group(1).title())
      index += 1
      continue

    if key == "skills":
      previous_key = _heading_key(_last_nonblank_line(output))
      if previous_key in {"competencies and", "competencies and skills"}:
        _remove_last_nonblank_line(output)
        previous_key = _heading_key(_last_nonblank_line(output))
      if not seen_skills:
        if previous_key in SKILLS_SUBSECTION_HEADINGS:
          _remove_last_nonblank_line(output)
          _append_description_line(output, "Skills")
          _append_description_line(output, "")
          _append_description_line(output, previous_key.title())
        else:
          _append_description_line(output, "Skills")
        seen_skills = True
      index += 1
      continue

    if _is_closing_date_heading(line):
      keys = _closing_date_keys_for_line(lines, index)
      if seen_closing_date:
        index = _skip_closing_date_value(lines, index, keys)
        continue
      seen_closing_date = True
      seen_closing_keys.update(keys)
      _append_description_line(output, line)
      index += 1
      continue

    if seen_closing_keys and _date_keys(line) & seen_closing_keys and _heading_key(_last_nonblank_line(output)) == "application":
      _remove_last_nonblank_line(output)
      index += 1
      continue

    _append_description_line(output, line)
    index += 1

  return _compact_description_lines(output)


def _append_group_join_link(lines: list[str], invite_url: str) -> None:
  invite = _clean_line(invite_url, 240)
  if not invite:
    return
  current = "\n".join(lines)
  if invite in current:
    return
  if lines and lines[-1] != "":
    lines.append("")
  lines.append(f"Join the group for more jobs: {invite}")


def format_group_feed_message(candidate: dict, invite_url: str = DEFAULT_GROUP_FEED_INVITE_URL) -> str:
  role = _job_title(candidate)
  location = _clean_line(candidate.get("location"), 180)
  company = _clean_line(candidate.get("company"), 180)
  closing = _clean_line(candidate.get("closing"), 120)
  description = _normalize_group_feed_description(
    _description_text(candidate.get("description")),
    role=role,
    location=location,
    company=company,
  )
  lines = [f"Job Title: {role}", ""]
  if location:
    lines.extend([f"Location: {location}", ""])
  if company and company.casefold() != "unknown employer" and not _description_has(description, company):
    lines.extend([f"Company: {company}", ""])
  if description:
    lines.extend([description, ""])

  application_lines: list[str] = []
  if closing and not _description_has(description, closing) and not _description_has_closing_date(description, closing):
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
  if application_lines:
    lines.extend(["Application", "", *application_lines])
  _append_group_join_link(lines, invite_url)
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
