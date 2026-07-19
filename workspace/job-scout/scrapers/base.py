#!/usr/bin/env python3
"""Shared helpers for the Job Scout board scrapers.

Standard-library only (urllib + re + html). The Job Scout container ships Python
3 stdlib + pymupdf and nothing else (no requests/bs4/lxml), so every scraper must
stay importable with the stdlib alone, mirroring job_scout.py and the existing
cron/algeria-exhibitions-listener/check_new_events.py poller.

Each board module produces candidate dicts via build_candidate(), which returns
the exact same shape as job_scout.normalized_candidate() so the merged list flows
through filter_candidates / dispatch unchanged.
"""

from __future__ import annotations

import datetime
import hashlib
import html as html_lib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from jobscout.constants import CANDIDATE_DESCRIPTION_CHAR_LIMIT

USER_AGENT = "Mozilla/5.0 (compatible; JobScout/1.0; +https://vacancymail.co.zw)"
DEFAULT_TIMEOUT = 15
DETAIL_TIMEOUT = 10
DEFAULT_ATTEMPTS = 2
RETRY_SECONDS = 2
# Be polite: small pause between requests to the same board and a hard ceiling on
# detail-page fetches per scraper run so a single dispatch can't hammer a site.
RATE_LIMIT_SECONDS = 0.7
MAX_DETAIL_FETCHES = 25

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _request(url: str, accept: str) -> urllib.request.Request:
  return urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})


def http_get(
  url: str,
  timeout: int = DEFAULT_TIMEOUT,
  attempts: int = DEFAULT_ATTEMPTS,
) -> str | None:
  """GET text with bounded retries. Returns the body or None on failure."""
  attempts = max(1, int(attempts))
  for attempt in range(attempts):
    try:
      with urllib.request.urlopen(_request(url, "text/html,*/*"), timeout=timeout) as response:
        return response.read().decode("utf-8", "ignore")
    except (urllib.error.URLError, OSError, ValueError):
      if attempt + 1 < attempts:
        time.sleep(RETRY_SECONDS)
        continue
      return None
  return None


def http_get_json(
  url: str,
  timeout: int = DEFAULT_TIMEOUT,
  attempts: int = DEFAULT_ATTEMPTS,
) -> object | None:
  """GET and parse JSON with bounded retries. Returns None after failure."""
  attempts = max(1, int(attempts))
  for attempt in range(attempts):
    try:
      with urllib.request.urlopen(_request(url, "application/json"), timeout=timeout) as response:
        body = response.read().decode("utf-8", "ignore")
      return json.loads(body) if body else None
    except (urllib.error.URLError, OSError, ValueError):
      if attempt + 1 < attempts:
        time.sleep(RETRY_SECONDS)
        continue
      return None
  return None


def http_post_form_json(
  url: str,
  payload: dict[str, object],
  timeout: int = DEFAULT_TIMEOUT,
  attempts: int = DEFAULT_ATTEMPTS,
) -> object | None:
  """POST a form and parse JSON with bounded retries."""
  data = urllib.parse.urlencode(payload).encode("utf-8")
  attempts = max(1, int(attempts))
  for attempt in range(attempts):
    request = _request(url, "application/json")
    request.data = data
    request.method = "POST"
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
      with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", "ignore")
      return json.loads(body) if body else None
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
      if attempt + 1 < attempts:
        time.sleep(RETRY_SECONDS)
        continue
      return None


def collapse_ws(value: object) -> str:
  return _WS_RE.sub(" ", str(value or "")).strip()


def strip_tags(value: object) -> str:
  """Drop HTML tags and unescape entities, collapsing whitespace."""
  return collapse_ws(_TAG_RE.sub(" ", html_lib.unescape(str(value or ""))))


def extract_emails(*values: object) -> list[str]:
  """Pull unique e-mail addresses out of arbitrary text (same regex as job_scout)."""
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


def absolute_url(base: str, href: str) -> str:
  return urllib.request.urljoin(base, str(href or "").strip())


def candidate_id(company: str, role: str, url: str) -> str:
  return hashlib.sha256(f"{company}\n{role}\n{url or ''}".encode("utf-8")).hexdigest()[:24]


def build_candidate(
  source: str,
  *,
  role: str,
  company: str,
  url: str,
  description: str = "",
  location: str = "",
  closing: str | None = None,
  emails: list[str] | None = None,
  apply_links: list[str] | None = None,
) -> dict:
  """Return a candidate dict matching job_scout.normalized_candidate exactly."""
  role = collapse_ws(role) or "Unknown role"
  company = collapse_ws(company) or "Unknown employer"
  url = str(url or "").strip()
  description = collapse_ws(description)

  emails = extract_emails(*(emails or []))
  links: list[str] = []
  for link in [url, *(apply_links or [])]:
    link = str(link or "").strip()
    if link and link not in links:
      links.append(link)

  return {
    "candidateId": candidate_id(company, role, url),
    "source": source,
    "sourceUrl": url or (links[0] if links else None),
    "applyLinks": links,
    "applicationEmails": emails,
    "applicationEmail": emails[0] if emails else None,
    "company": company,
    "role": role,
    "location": collapse_ws(location),
    "query": "",
    "closing": collapse_ws(closing) or None,
    "description": description[:CANDIDATE_DESCRIPTION_CHAR_LIMIT],
  }


def pagination_max(html: str, default: int = 1) -> int:
  """Read common ?page=N and /page/N/ pagination links from listing HTML."""
  pages = [int(value) for value in re.findall(r"(?:[?&]page=|/page/)([0-9]+)", str(html or ""), re.I)]
  return max([max(1, int(default)), *pages])


def month_lookback_start(count: int = 3, today: datetime.date | None = None) -> datetime.date:
  """Return the first day of the oldest included month."""
  current = today or datetime.date.today()
  year, month = current.year, current.month
  for _ in range(max(1, count) - 1):
    month -= 1
    if month == 0:
      month, year = 12, year - 1
  return datetime.date(year, month, 1)


def print_candidates(candidates: list[dict]) -> int:
  """Standalone-run helper: print candidates as JSON to stdout."""
  print(json.dumps({"count": len(candidates), "candidates": candidates}, indent=2, sort_keys=True))
  return 0


# Role/relevance matching lives in the sibling relevance module (which depends on
# collapse_ws above). Re-exported here so callers keep using base.<name> and the
# waitstaff/role constants stay reachable as base.WAITSTAFF_TRIGGERS etc. This
# import sits at the bottom so relevance can import this module back safely.
from .relevance import (  # noqa: E402
  ANY_ROLE_TRIGGERS,
  WAITSTAFF_CONTEXT,
  WAITSTAFF_TITLE_PHRASES,
  WAITSTAFF_TRIGGERS,
  build_candidate_matcher,
  build_role_matcher,
  jsearch_roles,
  keyword_match,
  role_terms,
  source_search_terms,
  uses_waitstaff_family,
)
