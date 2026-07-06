"""JSearch provider access and candidate normalization/filtering for Job Scout.

Includes the JSearch HTTP client, candidate shaping from JSearch and the scraped
Zimbabwe boards, deduplication, and the transient-failure exception used so a
JSearch outage falls back to the boards instead of aborting the run.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from genaie_env import env_default

from .constants import (
  DEFAULT_JSEARCH_COUNTRY,
  DEFAULT_JSEARCH_DATE_POSTED,
  DEFAULT_JSEARCH_LANGUAGE,
  DEFAULT_OPENWEBNINJA_BASE_URL,
  DEFAULT_RAPIDAPI_BASE_URL,
  DEFAULT_RAPIDAPI_HOST,
  JSEARCH_REQUEST_ATTEMPTS,
  JSEARCH_REQUEST_TIMEOUT,
  JSEARCH_RETRY_SECONDS,
  MAX_APPLICATION_ATTEMPTS,
  TERMINAL_APPLICATION_STATUSES,
)
from .profile import clean_items, preference_code, profile_values, search_queries, user_key
from .textutil import public_text


class JSearchUnavailable(RuntimeError):
  """Transient JSearch failure (HTTP/network). Non-fatal: the run continues with the
  scraped boards so a JSearch outage can't take the rest of dispatch down with it.
  Missing-credential errors stay as SystemExit (real misconfiguration, must be loud)."""


def jsearch_provider() -> str:
  requested = env_default("JSEARCH_PROVIDER", "auto").strip().lower() or "auto"
  if requested not in {"auto", "openwebninja", "rapidapi"}:
    raise SystemExit("JSEARCH_PROVIDER must be auto, openwebninja, or rapidapi")
  if requested != "auto":
    return requested
  if env_default("JSEARCH_API_KEY"):
    return "openwebninja"
  if env_default("JSEARCH_RAPIDAPI_KEY") or env_default("RAPIDAPI_KEY"):
    return "rapidapi"
  raise SystemExit("Missing JSearch credentials: set JSEARCH_API_KEY or JSEARCH_RAPIDAPI_KEY.")


def jsearch_request(query: str, country: str, language: str, limit: int, cursor: str | None = None) -> dict:
  provider = jsearch_provider()
  if provider == "rapidapi":
    key = env_default("JSEARCH_RAPIDAPI_KEY") or env_default("RAPIDAPI_KEY")
    if not key:
      raise SystemExit("Missing JSEARCH_RAPIDAPI_KEY.")
    base_url = env_default("JSEARCH_RAPIDAPI_BASE_URL", DEFAULT_RAPIDAPI_BASE_URL).rstrip("/")
    host = env_default("JSEARCH_RAPIDAPI_HOST", DEFAULT_RAPIDAPI_HOST)
    params = {
      "query": query,
      "country": country,
      "date_posted": env_default("JSEARCH_DATE_POSTED", DEFAULT_JSEARCH_DATE_POSTED),
      "num_pages": "1",
      "page": cursor or "1",
    }
    headers = {
      "X-RapidAPI-Key": key,
      "X-RapidAPI-Host": host,
    }
    url = f"{base_url}/search?{urllib.parse.urlencode(params)}"
  else:
    key = env_default("JSEARCH_API_KEY")
    if not key:
      raise SystemExit("Missing JSEARCH_API_KEY.")
    base_url = env_default("JSEARCH_BASE_URL", DEFAULT_OPENWEBNINJA_BASE_URL).rstrip("/")
    params = {
      "query": query,
      "country": country,
      "language": language,
      "date_posted": env_default("JSEARCH_DATE_POSTED", DEFAULT_JSEARCH_DATE_POSTED),
      "num": str(max(1, min(limit, 25))),
    }
    if cursor:
      params["cursor"] = cursor
    headers = {"x-api-key": key}
    url = f"{base_url}/search-v2?{urllib.parse.urlencode(params)}"

  for attempt in range(JSEARCH_REQUEST_ATTEMPTS):
    request = urllib.request.Request(url, headers={**headers, "Accept": "application/json"})
    try:
      with urllib.request.urlopen(request, timeout=JSEARCH_REQUEST_TIMEOUT) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else {}
    except urllib.error.HTTPError as err:
      body = err.read().decode("utf-8", errors="replace")
      print(f"JSearch request failed with HTTP {err.code}: {body[:1000]}", file=sys.stderr)
      if 500 <= err.code < 600 and attempt + 1 < JSEARCH_REQUEST_ATTEMPTS:
        time.sleep(JSEARCH_RETRY_SECONDS)
        continue
      raise JSearchUnavailable(f"HTTP {err.code}") from err
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as err:
      # DNS, connection, read-timeout, and malformed-response failures are transient.
      print(f"Could not reach JSearch API: {err}", file=sys.stderr)
      if attempt + 1 < JSEARCH_REQUEST_ATTEMPTS:
        time.sleep(JSEARCH_RETRY_SECONDS)
        continue
      raise JSearchUnavailable("network error") from err
  raise JSearchUnavailable("network error")


def jsearch_items(response: dict) -> list[dict]:
  for key in ("data", "jobs", "results"):
    value = response.get(key)
    if isinstance(value, list):
      return [item for item in value if isinstance(item, dict)]
  return []


EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)


def extract_emails(*values: object) -> list[str]:
  emails: list[str] = []
  seen: set[str] = set()
  for value in values:
    for email in EMAIL_RE.findall(str(value or "")):
      key = email.casefold()
      if key in seen:
        continue
      seen.add(key)
      emails.append(email)
  return emails


def apply_links(item: dict) -> list[str]:
  links: list[str] = []
  for key in ("job_apply_link", "job_google_link", "employer_website"):
    value = str(item.get(key) or "").strip()
    if value:
      links.append(value)
  options = item.get("apply_options")
  if isinstance(options, list):
    for option in options:
      if not isinstance(option, dict):
        continue
      value = str(option.get("apply_link") or option.get("link") or "").strip()
      if value:
        links.append(value)
  output: list[str] = []
  seen: set[str] = set()
  for link in links:
    if link in seen:
      continue
    seen.add(link)
    output.append(link)
  return output


def normalized_candidate(item: dict, query: str) -> dict:
  company = re.sub(r"\s+", " ", str(item.get("employer_name") or item.get("company") or "Unknown employer")).strip()
  role = re.sub(r"\s+", " ", str(item.get("job_title") or item.get("title") or "Unknown role")).strip()
  links = apply_links(item)
  description = re.sub(r"\s+", " ", str(item.get("job_description") or item.get("description") or "")).strip()
  emails = extract_emails(description, item.get("job_apply_link"), item.get("apply_options"))
  location = ", ".join(clean_items([
    item.get("job_city"),
    item.get("job_state"),
    item.get("job_country"),
  ]))
  source_url = links[0] if links else None
  raw_id = str(item.get("job_id") or item.get("id") or "").strip()
  closing = item.get("job_offer_expiration_datetime_utc") or item.get("job_offer_expiration_datetime")
  if not closing and item.get("job_offer_expiration_timestamp") is not None:
    try:
      closing = datetime.fromtimestamp(float(item["job_offer_expiration_timestamp"]), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
      closing = None
  fallback_id = hashlib.sha256(f"{company}\n{role}\n{source_url or ''}".encode("utf-8")).hexdigest()[:24]
  return {
    "candidateId": raw_id or fallback_id,
    "source": "jsearch",
    "sourceUrl": source_url,
    "applyLinks": links,
    "applicationEmails": emails,
    "applicationEmail": emails[0] if emails else None,
    "company": company,
    "role": role,
    "location": location,
    "query": query,
    "closing": closing,
    "description": description[:2000],
  }


def candidate_key(candidate: dict) -> str:
  return hashlib.sha256(
    "\n".join([
      str(candidate.get("company") or "").casefold(),
      str(candidate.get("role") or "").casefold(),
      str(candidate.get("sourceUrl") or "").casefold(),
      str(candidate.get("candidateId") or "").casefold(),
    ]).encode("utf-8")
  ).hexdigest()


def application_key(value: dict) -> tuple[str, str]:
  return (
    str(value.get("company") or "").strip().casefold(),
    str(value.get("role") or "").strip().casefold(),
  )


def closing_state(value: object, now: datetime | None = None) -> str:
  text = str(value or "").strip()
  if not text:
    return "unknown"
  current = now or datetime.now(timezone.utc)
  cleaned = re.sub(r"^(?:expires?|due|closing(?: date)?|deadline)\s*:?\s*", "", text, flags=re.I).strip()
  for fmt in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%d %B, %Y", "%d %b, %Y"):
    try:
      closing_date = datetime.strptime(cleaned, fmt).date()
      return "expired" if closing_date < current.date() else "open"
    except ValueError:
      continue
  try:
    parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
      parsed = parsed.replace(tzinfo=timezone.utc)
    return "expired" if parsed < current else "open"
  except ValueError:
    pass
  return "malformed"


def filter_candidates_with_stats(
  candidates: list[dict],
  preferences: dict,
  applications: list[dict],
  limit: int,
) -> tuple[list[dict], dict[str, dict[str, int]]]:
  exclusions = [item.casefold() for item in clean_items(profile_values(preferences, "exclusions"))]
  existing: set[tuple[str, str]] = set()
  for application in applications:
    status = str(application.get("status") or "").strip().lower()
    try:
      attempts = int(application.get("attemptCount") or 0)
    except (TypeError, ValueError):
      attempts = 0
    # Historical records without a status and manual-submission records are
    # deliberately terminal: users may already have applied after receiving the URL.
    if not status or status in TERMINAL_APPLICATION_STATUSES:
      existing.add(application_key(application))
    elif status == "failed" and attempts >= MAX_APPLICATION_ATTEMPTS:
      existing.add(application_key(application))
  seen: set[str] = set()
  output: list[dict] = []
  stats: dict[str, dict[str, int]] = {}
  for candidate in candidates:
    source = str(candidate.get("source") or "unknown")
    source_stats = stats.setdefault(source, {
      "eligibleCount": 0,
      "expiredCount": 0,
      "duplicateCount": 0,
      "excludedCount": 0,
      "unknownDeadlineCount": 0,
      "malformedDeadlineCount": 0,
    })
    deadline = closing_state(candidate.get("closing"))
    if deadline == "expired":
      source_stats["expiredCount"] += 1
      continue
    if deadline == "unknown":
      source_stats["unknownDeadlineCount"] += 1
    elif deadline == "malformed":
      source_stats["malformedDeadlineCount"] += 1
    text = " ".join([
      str(candidate.get("company") or ""),
      str(candidate.get("role") or ""),
      str(candidate.get("description") or ""),
    ]).casefold()
    if any(exclusion in text for exclusion in exclusions):
      source_stats["excludedCount"] += 1
      continue
    appkey = application_key(candidate)
    # Skip jobs already applied to (prior runs) and collapse same company/role
    # across sources within this run, so a JSearch + board duplicate is applied once.
    if appkey in existing:
      source_stats["duplicateCount"] += 1
      continue
    key = candidate_key(candidate)
    if key in seen:
      source_stats["duplicateCount"] += 1
      continue
    if len(output) >= limit:
      continue
    seen.add(key)
    existing.add(appkey)
    output.append(candidate)
    source_stats["eligibleCount"] += 1
  return output, stats


def filter_candidates(candidates: list[dict], preferences: dict, applications: list[dict], limit: int) -> list[dict]:
  output, _ = filter_candidates_with_stats(candidates, preferences, applications, limit)
  return output


def apply_source_filter_stats(source_results: dict[str, dict], filter_stats: dict[str, dict[str, int]]) -> None:
  for source, stats in filter_stats.items():
    result = source_results.setdefault(source, {"status": "ok", "rawCount": 0, "durationMs": 0})
    for key, value in stats.items():
      if key == "expiredCount":
        result[key] = int(result.get(key) or 0) + value
      else:
        result[key] = value
  for result in source_results.values():
    for key in (
      "eligibleCount",
      "expiredCount",
      "duplicateCount",
      "excludedCount",
      "unknownDeadlineCount",
      "malformedDeadlineCount",
    ):
      result.setdefault(key, 0)


def fetch_jsearch_candidates_with_result(preferences: dict, limit: int) -> tuple[list[dict], dict]:
  started = time.monotonic()
  country = preference_code(preferences, "country", DEFAULT_JSEARCH_COUNTRY)
  language = preference_code(preferences, "language", DEFAULT_JSEARCH_LANGUAGE)
  candidates: list[dict] = []
  raw_count = 0
  expired_count = 0
  roles = clean_items(profile_values(preferences, "targetRoles") or profile_values(preferences, "roles"))
  try:
    from scrapers import base as scraper_base
    match = scraper_base.build_candidate_matcher(roles) if scraper_base.uses_waitstaff_family(roles) else lambda role, text: True
  except Exception:
    match = lambda role, text: True
  for query in search_queries(preferences):
    try:
      response = jsearch_request(query, country, language, limit)
    except JSearchUnavailable as err:
      print(f"JSearch unavailable, continuing without it: {public_text(err)}", file=sys.stderr)
      return candidates, {
        "status": "unavailable",
        "rawCount": raw_count,
        "error": public_text(err),
        "durationMs": round((time.monotonic() - started) * 1000),
      }
    items = jsearch_items(response)
    raw_count += len(items)
    for item in items:
      if item.get("job_is_expired") is True or str(item.get("job_is_expired") or "").casefold() == "true":
        expired_count += 1
        continue
      candidate = normalized_candidate(item, query)
      if match(str(candidate.get("role") or ""), str(candidate.get("description") or "")):
        candidates.append(candidate)
    if len(candidates) >= limit:
      break
  return candidates, {
    "status": "ok",
    "rawCount": raw_count,
    "matchedCount": len(candidates),
    "expiredCount": expired_count,
    "durationMs": round((time.monotonic() - started) * 1000),
  }


def fetch_jsearch_candidates(preferences: dict, limit: int) -> list[dict]:
  candidates, _ = fetch_jsearch_candidates_with_result(preferences, limit)
  return candidates


# Zimbabwe job boards (VacancyMail, iHarare, VacancyBox) are scraped to extend JSearch
# for ZW-targeting subscribers only. JSearch still runs for everyone.
ZIMBABWE_HINTS = {
  "zimbabwe", "harare", "bulawayo", "mutare", "gweru", "kwekwe", "masvingo",
  "chitungwiza", "kadoma", "chinhoyi", "marondera", "victoria falls", "zvishavane",
  "bindura", "beitbridge", "hwange", "redcliff", "norton", "ruwa", "chegutu",
  "kariba", "rusape", "chiredzi", "gwanda", "midlands",
}


def zw_targeted(preferences: dict) -> bool:
  country_raw = str(preferences.get("country") or "").strip().casefold()
  if country_raw == "zw" or "zimbabwe" in country_raw:
    return True
  if preference_code(preferences, "country", DEFAULT_JSEARCH_COUNTRY) == "zw":
    return True
  for location in clean_items(profile_values(preferences, "locations")):
    text = location.casefold()
    if any(hint in text for hint in ZIMBABWE_HINTS):
      return True
  return False


def fetch_scraped_candidates(preferences: dict, limit: int) -> list[dict]:
  """Board-scraped candidates for ZW-targeting users. Never raises; merged alongside
  JSearch results. Returns [] when not ZW-targeted or the scrapers are unavailable."""
  if limit <= 0 or not zw_targeted(preferences):
    return []
  try:
    import scrapers  # lazily imported so a scraper import error can't break JSearch
    return scrapers.fetch_scraped_candidates(preferences, limit)
  except Exception as err:
    print(f"Scraper sources failed: {public_text(err)}", file=sys.stderr)
    return []


def fetch_scraped_candidates_with_results(preferences: dict, limit: int) -> tuple[list[dict], dict[str, dict]]:
  if limit <= 0 or not zw_targeted(preferences):
    return [], {
      name: {"status": "skipped", "rawCount": 0, "durationMs": 0}
      for name in ("vacancymail", "iharare", "vacancybox", "applynow")
    }
  try:
    import scrapers
    return scrapers.fetch_scraped_candidates_with_results(preferences, limit)
  except Exception as err:
    message = public_text(err, 180)
    return [], {
      name: {"status": "unavailable", "rawCount": 0, "error": message, "durationMs": 0}
      for name in ("vacancymail", "iharare", "vacancybox", "applynow")
    }


def fetch_zimlinks_candidates(preferences: dict, limit: int) -> list[dict]:
  """Read fresh channel candidates first for Zimbabwe-targeted subscribers."""
  if limit <= 0 or not zw_targeted(preferences):
    return []
  try:
    from scrapers import base as scraper_base, target_roles, zimlinks
    match = scraper_base.build_candidate_matcher(target_roles(preferences))
    return zimlinks.fetch(limit=limit, match=match)
  except Exception as err:
    print(f"ZimLinks source failed: {public_text(err)}", file=sys.stderr)
    return []


def fetch_zimlinks_candidates_with_result(preferences: dict, limit: int) -> tuple[list[dict], dict]:
  if limit <= 0 or not zw_targeted(preferences):
    return [], {"status": "skipped", "rawCount": 0, "durationMs": 0}
  started = time.monotonic()
  try:
    from scrapers import base as scraper_base, target_roles, zimlinks
    match = scraper_base.build_candidate_matcher(target_roles(preferences))
    candidates = zimlinks.fetch(limit=limit, match=match)
    return candidates, {
      "status": "ok",
      "rawCount": len(candidates),
      "durationMs": round((time.monotonic() - started) * 1000),
    }
  except Exception as err:
    print(f"ZimLinks source failed: {public_text(err)}", file=sys.stderr)
    return [], {
      "status": "unavailable",
      "rawCount": 0,
      "error": public_text(err, 180),
      "durationMs": round((time.monotonic() - started) * 1000),
    }


def fetch_candidate_sources(preferences: dict, limit: int) -> tuple[list[dict], dict[str, dict]]:
  zimlinks, zimlinks_result = fetch_zimlinks_candidates_with_result(preferences, limit)
  jsearch, jsearch_result = fetch_jsearch_candidates_with_result(preferences, limit)
  boards, board_results = fetch_scraped_candidates_with_results(preferences, limit)
  results = {
    "zimlinks-whatsapp": zimlinks_result,
    "jsearch": jsearch_result,
    **board_results,
  }
  for result in results.values():
    if result.get("error"):
      result["error"] = public_text(result["error"], 180)
  return zimlinks + jsearch + boards, results


def register_channel_candidate_eligibility(run_id: str, user_id: str, candidates: list[dict]) -> None:
  """Record selected channel candidates without making source bookkeeping fatal."""
  try:
    from scrapers import zimlinks
    zimlinks.register_eligibility(run_id, user_key(user_id), candidates)
  except Exception as err:
    print(f"ZimLinks eligibility tracking failed: {public_text(err)}", file=sys.stderr)


def record_channel_candidate_outcome(
  run_id: str,
  user_id: str,
  candidate: dict,
  outcome: str,
  attempt_count: int = 0,
) -> None:
  try:
    from scrapers import zimlinks
    zimlinks.record_outcome(run_id, user_key(user_id), candidate, outcome, attempt_count)
  except Exception as err:
    print(f"ZimLinks outcome tracking failed: {public_text(err)}", file=sys.stderr)


def finalize_channel_candidate_dispatch(run_id: str) -> None:
  try:
    from scrapers import zimlinks
    zimlinks.finalize_dispatch(run_id)
  except Exception as err:
    print(f"ZimLinks cleanup failed: {public_text(err)}", file=sys.stderr)


