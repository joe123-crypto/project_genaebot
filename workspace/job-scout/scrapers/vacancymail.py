#!/usr/bin/env python3
"""Scraper for VacancyMail Zimbabwe (https://vacancymail.co.zw/jobs/).

Listing page renders one anchor card per job:

    <a href="/jobs/<slug>/" class="job-listing">
      <h3 class="job-listing-title">Role</h3>
      <h4 class="job-listing-company">Company</h4>
      <p class="job-listing-text">short blurb</p>
      <div class="job-listing-footer"><ul>
        <li><i class="icon-material-outline-location-on"></i> Harare</li>
        <li><i class="icon-material-outline-access-time"></i> Expires 20 Jun 2026</li>
      </ul></div>
    </a>

The detail page carries the full description + a "How to Apply" block that usually
contains an application e-mail. Std-lib only.
"""

from __future__ import annotations

import argparse
import sys
import time

if __package__ in (None, ""):
  sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__file__)))
  from scrapers import base
else:
  from . import base

SOURCE = "vacancymail"
LIST_URL = "https://vacancymail.co.zw/jobs/"
BASE_URL = "https://vacancymail.co.zw"

import re

CARD_RE = re.compile(
  r'<a\s+href="(?P<href>/jobs/[^"]+)"\s+class="job-listing"[^>]*>(?P<body>.*?)</a>',
  re.S,
)
TITLE_RE = re.compile(r'<h3[^>]*class="job-listing-title"[^>]*>(.*?)</h3>', re.S)
COMPANY_RE = re.compile(r'<h4[^>]*class="job-listing-company"[^>]*>(.*?)</h4>', re.S)
TEXT_RE = re.compile(r'<p[^>]*class="job-listing-text"[^>]*>(.*?)</p>', re.S)
LOCATION_RE = re.compile(r'icon-material-outline-location-on[^>]*></i>\s*(.*?)</li>', re.S)
EXPIRES_RE = re.compile(r'Expires\s+([0-9]{1,2}\s+\w+\s+\d{4})', re.I)
# Detail page: the single job sits in the left content column; keep parsing within
# it so we don't scoop the site footer's generic e-mail.
CONTENT_RE = re.compile(r'content-right-offset"(.*?)(?:<div class="col-xl-4|<div class="sidebar|</section>)', re.S)
SECTION_RE = re.compile(r'<div class="single-page-section">(.*?)</div>\s*(?=<div class="single-page-section">|$)', re.S)


def parse_cards(html: str) -> list[dict]:
  cards: list[dict] = []
  for match in CARD_RE.finditer(html):
    body = match.group("body")
    title = TITLE_RE.search(body)
    if not title:
      continue
    company = COMPANY_RE.search(body)
    text = TEXT_RE.search(body)
    location = LOCATION_RE.search(body)
    expires = EXPIRES_RE.search(body)
    cards.append({
      "url": base.absolute_url(BASE_URL, match.group("href")),
      "role": base.strip_tags(title.group(1)),
      "company": base.strip_tags(company.group(1)) if company else "",
      "blurb": base.strip_tags(text.group(1)) if text else "",
      "location": base.strip_tags(location.group(1)) if location else "",
      "closing": base.strip_tags(expires.group(1)) if expires else None,
    })
  return cards


def parse_detail(html: str) -> tuple[str, list[str]]:
  """Return (description, emails) from a job detail page."""
  region = CONTENT_RE.search(html)
  scope = region.group(1) if region else html
  sections = SECTION_RE.findall(scope)
  description = base.strip_tags(" ".join(sections)) if sections else base.strip_tags(scope)
  emails = base.extract_emails(scope)
  return description, emails


def fetch(limit: int = 15, timeout: int = base.DEFAULT_TIMEOUT, match=None) -> list[dict]:
  html = base.http_get(LIST_URL, timeout=timeout)
  if not html:
    return []
  candidates: list[dict] = []
  fetched = 0
  for card in parse_cards(html):
    if len(candidates) >= max(0, limit):
      break
    if match is not None and not match(card["role"], f"{card['role']} {card['blurb']}"):
      continue
    description = card["blurb"]
    emails: list[str] = []
    if fetched < base.MAX_DETAIL_FETCHES:
      detail = base.http_get(card["url"], timeout=timeout)
      if detail:
        detail_desc, emails = parse_detail(detail)
        if detail_desc:
          description = detail_desc
      fetched += 1
      time.sleep(base.RATE_LIMIT_SECONDS)
    candidates.append(base.build_candidate(
      SOURCE,
      role=card["role"],
      company=card["company"],
      url=card["url"],
      description=description,
      location=card["location"],
      closing=card["closing"],
      emails=emails,
    ))
  return candidates


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(prog="vacancymail")
  parser.add_argument("--limit", type=int, default=10)
  parser.add_argument("--query", help="optional case-insensitive role/keyword filter")
  args = parser.parse_args(argv)
  match = base.keyword_match(args.query) if args.query else None
  return base.print_candidates(fetch(limit=args.limit, match=match))


if __name__ == "__main__":
  raise SystemExit(main())
