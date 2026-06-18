#!/usr/bin/env python3
"""Scraper for VacancyBox (https://vacancybox.co.zw/).

VacancyBox runs WP Job Manager; real vacancies are the `job_listing` post type,
exposed over the WordPress REST API at /wp-json/wp/v2/job-listings (the plain
/wp/v2/posts feed is blog/advice content, not jobs). The REST item already carries
the title, permalink, post date and full HTML body (with the application e-mail and
a "DUE: <date>" closing line); the company name and clean location come from the
detail page's WP Job Manager markup. Std-lib only.
"""

from __future__ import annotations

import argparse
import re
import sys
import time

if __package__ in (None, ""):
  sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__file__)))
  from scrapers import base
else:
  from . import base

SOURCE = "vacancybox"
API_URL = "https://vacancybox.co.zw/wp-json/wp/v2/job-listings"

DUE_RE = re.compile(r'DUE:\s*([0-9]{1,2}\s+\w+\s+\d{4})', re.I)
NAME_RE = re.compile(r'<p class="name">\s*<strong>(.*?)</strong>', re.S)
LOGO_ALT_RE = re.compile(r'class="company_logo"[^>]*alt="([^"]*)"', re.I)
LOCATION_RE = re.compile(r'<li class="location">(.*?)</li>', re.S)
# Sponsored / CV-service boilerplate that WP injects at the top of every body.
NOISE_RE = re.compile(
  r'(Sponsored|CV Writing Services.*?5329|WhatsApp:\s*\+?[0-9 ]+)',
  re.I | re.S,
)


def _rendered(value: object) -> str:
  if isinstance(value, dict):
    return str(value.get("rendered") or "")
  return str(value or "")


def parse_detail(html: str) -> tuple[str, str]:
  """Return (company, location) from a VacancyBox job detail page."""
  name = NAME_RE.search(html) or LOGO_ALT_RE.search(html)
  company = base.strip_tags(name.group(1)) if name else ""
  location = LOCATION_RE.search(html)
  return company, base.strip_tags(location.group(1)) if location else ""


def candidate_from_item(item: dict, *, timeout: int, fetch_detail: bool) -> dict:
  url = str(item.get("link") or "").strip()
  role = base.strip_tags(_rendered(item.get("title")))
  body_html = _rendered(item.get("content"))
  description = base.strip_tags(NOISE_RE.sub(" ", body_html))
  emails = base.extract_emails(body_html)
  due = DUE_RE.search(base.strip_tags(body_html))
  closing = due.group(1) if due else None

  company = ""
  location = ""
  if fetch_detail and url:
    detail = base.http_get(url, timeout=timeout)
    if detail:
      company, location = parse_detail(detail)
    time.sleep(base.RATE_LIMIT_SECONDS)

  return base.build_candidate(
    SOURCE,
    role=role,
    company=company,
    url=url,
    description=description,
    location=location,
    closing=closing,
    emails=emails,
  )


def fetch(limit: int = 15, timeout: int = base.DEFAULT_TIMEOUT, match=None) -> list[dict]:
  # Pull a wider page than `limit` so role filtering still has stock to draw from;
  # detail pages are only fetched for items that already pass the keyword match.
  per_page = max(1, min(max(limit * 3, limit), 50))
  # Trim the REST payload to the fields we use (skips heavy yoast_head blocks etc.).
  data = base.http_get_json(
    f"{API_URL}?per_page={per_page}&orderby=date&order=desc&_fields=title,link,date,content",
    timeout=timeout,
  )
  if not isinstance(data, list):
    return []
  candidates: list[dict] = []
  fetched = 0
  for item in data:
    if not isinstance(item, dict):
      continue
    if len(candidates) >= max(0, limit):
      break
    role = base.strip_tags(_rendered(item.get("title")))
    body = base.strip_tags(_rendered(item.get("content")))
    if match is not None and not match(role, f"{role} {body}"):
      continue
    candidates.append(candidate_from_item(
      item,
      timeout=timeout,
      fetch_detail=fetched < base.MAX_DETAIL_FETCHES,
    ))
    fetched += 1
  return candidates


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(prog="vacancybox")
  parser.add_argument("--limit", type=int, default=10)
  parser.add_argument("--query", help="optional case-insensitive role/keyword filter")
  args = parser.parse_args(argv)
  match = base.keyword_match(args.query) if args.query else None
  return base.print_candidates(fetch(limit=args.limit, match=match))


if __name__ == "__main__":
  raise SystemExit(main())
