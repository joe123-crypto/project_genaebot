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

import hashlib
import html as html_lib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "Mozilla/5.0 (compatible; JobScout/1.0; +https://vacancymail.co.zw)"
DEFAULT_TIMEOUT = 25
# Be polite: small pause between requests to the same board and a hard ceiling on
# detail-page fetches per scraper run so a single dispatch can't hammer a site.
RATE_LIMIT_SECONDS = 0.7
MAX_DETAIL_FETCHES = 25

WAITSTAFF_TRIGGERS = {"waitress", "waiter", "wait staff", "waitstaff"}
WAITSTAFF_SEARCH_ROLE = "restaurant jobs"
WAITSTAFF_TITLE_PHRASES = (
  "waitress",
  "waiter",
  "wait staff",
  "waitstaff",
  "restaurant server",
  "restaurant staff",
  "food and beverage attendant",
  "food service attendant",
)
WAITSTAFF_CONTEXT = ("restaurant", "hotel", "hospitality", "food", "beverage", "dining", "lodge")
ANY_ROLE_TRIGGERS = {"any", "any job", "anything"}

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _request(url: str, accept: str) -> urllib.request.Request:
  return urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})


def http_get(url: str, timeout: int = DEFAULT_TIMEOUT) -> str | None:
  """GET text with one retry. Returns the body or None on failure (never raises)."""
  for attempt in range(2):
    try:
      with urllib.request.urlopen(_request(url, "text/html,*/*"), timeout=timeout) as response:
        return response.read().decode("utf-8", "ignore")
    except (urllib.error.URLError, OSError, ValueError):
      if attempt == 0:
        time.sleep(2)
        continue
      return None
  return None


def http_get_json(url: str, timeout: int = DEFAULT_TIMEOUT) -> object | None:
  """GET and parse JSON with one retry. Returns the decoded object or None."""
  for attempt in range(2):
    try:
      with urllib.request.urlopen(_request(url, "application/json"), timeout=timeout) as response:
        body = response.read().decode("utf-8", "ignore")
      return json.loads(body) if body else None
    except (urllib.error.URLError, OSError, ValueError):
      if attempt == 0:
        time.sleep(2)
        continue
      return None
  return None


def http_post_form_json(url: str, payload: dict[str, object], timeout: int = DEFAULT_TIMEOUT) -> object | None:
  """POST a form and parse JSON with one retry. Returns None after a bounded failure."""
  data = urllib.parse.urlencode(payload).encode("utf-8")
  for attempt in range(2):
    request = _request(url, "application/json")
    request.data = data
    request.method = "POST"
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
      with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", "ignore")
      return json.loads(body) if body else None
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
      if attempt == 0:
        time.sleep(2)
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
    "description": description[:2000],
  }


# Generic words that should not, on their own, qualify a board listing as relevant.
_STOPWORDS = {
  "and", "the", "for", "with", "job", "jobs", "vacancy", "vacancies",
  "post", "posts", "needed", "wanted", "required", "available",
}
_WORD_RE = re.compile(r"[a-z0-9]+")


def role_terms(phrase: str) -> list[str]:
  """Significant lowercase words of a role phrase (len >= 3, excluding stopwords)."""
  words = [w for w in _WORD_RE.findall(str(phrase or "").casefold()) if len(w) >= 3 and w not in _STOPWORDS]
  if words:
    return words
  collapsed = collapse_ws(phrase).casefold()
  return [collapsed] if collapsed else []


def build_role_matcher(roles: list[str]):
  """Return predicate(role, text) -> bool. A listing matches when, for any target
  role, all of that role's significant words appear in the listing role+text. With
  no usable roles the matcher accepts everything (board feed, unfiltered)."""
  term_sets = [terms for terms in (role_terms(r) for r in (roles or [])) if terms]
  if not term_sets:
    return lambda role, text: True

  def match(role: str, text: str) -> bool:
    haystack = f"{role} {text}".casefold()
    return any(all(term in haystack for term in terms) for terms in term_sets)

  return match


def uses_waitstaff_family(roles: list[str]) -> bool:
  normalized = {collapse_ws(role).casefold() for role in (roles or [])}
  return bool(normalized & WAITSTAFF_TRIGGERS)


def jsearch_roles(roles: list[str]) -> list[str]:
  """Collapse a waitstaff family to one broad query so aliases do not multiply API calls."""
  output: list[str] = []
  waitstaff_added = False
  for role in roles or []:
    normalized = collapse_ws(role)
    if normalized.casefold() in WAITSTAFF_TRIGGERS:
      if not waitstaff_added:
        output.append(WAITSTAFF_SEARCH_ROLE)
        waitstaff_added = True
    elif normalized:
      output.append(normalized)
  return output


def build_candidate_matcher(roles: list[str]):
  """Match configured roles, including a guarded hospitality waitstaff family."""
  normalized_roles = {collapse_ws(role).casefold() for role in (roles or [])}
  if normalized_roles & ANY_ROLE_TRIGGERS:
    return lambda role, text: True
  ordinary = [role for role in (roles or []) if collapse_ws(role).casefold() not in WAITSTAFF_TRIGGERS]
  ordinary_match = build_role_matcher(ordinary) if ordinary else None
  waitstaff = uses_waitstaff_family(roles)

  def match(role: str, text: str) -> bool:
    if ordinary_match is not None and ordinary_match(role, text):
      return True
    if not waitstaff:
      return ordinary_match is None
    title = collapse_ws(role).casefold()
    context = f"{title} {collapse_ws(text).casefold()}"
    if any(phrase in title for phrase in WAITSTAFF_TITLE_PHRASES):
      return True
    if re.search(r"\b(?:server|staff)\b", title) and any(term in context for term in WAITSTAFF_CONTEXT):
      return True
    return False

  return match


def keyword_match(query: str):
  """Single-keyword/role predicate for standalone CLI use."""
  return build_role_matcher([query])


def print_candidates(candidates: list[dict]) -> int:
  """Standalone-run helper: print candidates as JSON to stdout."""
  print(json.dumps({"count": len(candidates), "candidates": candidates}, indent=2, sort_keys=True))
  return 0
