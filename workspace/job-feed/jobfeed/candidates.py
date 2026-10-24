"""Job advert candidates for the feed: fetch them from the job boards and judge them.

The boards (VacancyMail, iHarare, VacancyBox, ApplyNow) are scraped by the
`scrapers` package; this module wraps that fetch so one failing board or a
scraper import error never sinks the run, and holds the small predicates the
feed uses to drop expired, title-only and duplicate adverts.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
import urllib.parse

from .constants import CANDIDATE_DESCRIPTION_CHAR_LIMIT, UNKNOWN_EMPLOYER
from .textutil import public_text


BOARD_SOURCES = ("vacancymail", "iharare", "vacancybox", "applynow")


def application_key(value: dict) -> tuple[str, str]:
  return (
    str(value.get("company") or "").strip().casefold(),
    str(value.get("role") or "").strip().casefold(),
  )


def is_actionable_application_url(value: object) -> bool:
  link = str(value or "").strip()
  if not link.startswith(("https://", "http://")):
    return False
  try:
    host = (urllib.parse.urlsplit(link).hostname or "").casefold().rstrip(".")
  except ValueError:
    return False
  if not host:
    return False
  return host != "wa.me" and host != "whatsapp.com" and not host.endswith(".whatsapp.com")


def candidate_application_url(candidate: dict) -> str | None:
  links = candidate.get("applyLinks")
  if isinstance(links, list):
    for value in links:
      link = str(value or "").strip()
      if is_actionable_application_url(link):
        return link
  source = str(candidate.get("sourceUrl") or "").strip()
  return source if is_actionable_application_url(source) else None


LOW_INFORMATION_DESCRIPTION_CHAR_LIMIT = 80


def _candidate_clean_text(value: object, limit: int = CANDIDATE_DESCRIPTION_CHAR_LIMIT) -> str:
  text = re.sub(r"<[^>]+>", " ", str(value or ""))
  text = re.sub(r"[\r\n\t]+", " ", text)
  text = re.sub(r"\s+", " ", text).strip()
  return text[:limit]


def _has_non_empty_value(value: object) -> bool:
  if isinstance(value, (list, tuple, set)):
    return any(str(item or "").strip() for item in value)
  return bool(str(value or "").strip())


def is_low_information_candidate(candidate: dict) -> bool:
  """True for title-only vacancies that do not provide enough data to act on."""
  description = _candidate_clean_text(candidate.get("description"))
  if len(description) >= LOW_INFORMATION_DESCRIPTION_CHAR_LIMIT:
    return False

  company = _candidate_clean_text(candidate.get("company"), 240).casefold()
  known_company = bool(company) and company != UNKNOWN_EMPLOYER.casefold()
  if known_company:
    return False
  if _has_non_empty_value(candidate.get("location")):
    return False
  if _has_non_empty_value(candidate.get("closing")):
    return False
  if (
    _has_non_empty_value(candidate.get("applicationEmail"))
    or _has_non_empty_value(candidate.get("applicationEmails"))
  ):
    return False
  if (
    _has_non_empty_value(candidate.get("applyLinks"))
    or _has_non_empty_value(candidate.get("contactLinks"))
  ):
    return False
  if candidate_application_url(candidate):
    return False
  return True


def has_application_email(candidate: dict) -> bool:
  if str(candidate.get("applicationEmail") or "").strip():
    return True
  values = candidate.get("applicationEmails")
  return isinstance(values, list) and any(str(value or "").strip() for value in values)


def closing_state(value: object, now: datetime | None = None) -> str:
  text = str(value or "").strip()
  if not text:
    return "unknown"
  current = now or datetime.now(timezone.utc)
  cleaned = re.sub(r"^(?:expires?|due|closing(?: date)?|deadline)\s*:?\s*", "", text, flags=re.I).strip()
  cleaned = re.sub(
    r"^(?:Monday|Mon|Tuesday|Tue|Tues|Wednesday|Wed|Thursday|Thu|Thur|Thurs|"
    r"Friday|Fri|Saturday|Sat|Sunday|Sun)\.?,?\s+",
    "",
    cleaned,
    flags=re.I,
  ).strip()
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


def fetch_scraped_candidates_with_results(preferences: dict, limit: int) -> tuple[list[dict], dict[str, dict]]:
  if limit <= 0:
    return [], {
      name: {"status": "skipped", "rawCount": 0, "durationMs": 0}
      for name in BOARD_SOURCES
    }
  try:
    import scrapers
    return scrapers.fetch_scraped_candidates_with_results(preferences, limit)
  except Exception as err:
    message = public_text(err, 180)
    return [], {
      name: {"status": "unavailable", "rawCount": 0, "error": message, "durationMs": 0}
      for name in BOARD_SOURCES
    }
