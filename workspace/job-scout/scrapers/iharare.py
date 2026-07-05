#!/usr/bin/env python3
"""Scraper for iHarare Jobs (https://ihararejobs.com/jobs/).

Listing cards live in a company-list block:

    <div class="company-list-details">
      <h3><a href="/job/<slug>-<id>/">ROLE TITLE</a></h3>
      <p class="company-state"><i class="fa fa-map-marker"></i> Gweru</p>
      <p class="open-icon"><i class="fa fa-clock-o"></i> Expires 22 Jun 2026</p>
      <p class="company-state"><i class="fa fa-building"></i> Company Name</p>
    </div>

The detail page holds the full description inside .single-candidate-bottom-left and
may carry an application e-mail and/or an external "apply" link. Std-lib only.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.parse

if __package__ in (None, ""):
  sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__file__)))
  from scrapers import base
else:
  from . import base

SOURCE = "iharare"
LIST_URL = "https://ihararejobs.com/jobs/"
BASE_URL = "https://ihararejobs.com"

CARD_RE = re.compile(
  r'<h3><a href="(?P<href>/job/[^"]+)">(?P<title>.*?)</a></h3>(?P<rest>.*?)'
  r'(?=<h3><a href="/job/|<div class="company-list-logo|$)',
  re.S,
)
MAP_RE = re.compile(r'fa-map-marker[^>]*></i>\s*(.*?)</p>', re.S)
BUILDING_RE = re.compile(r'fa-building[^>]*></i>\s*(.*?)</p>', re.S)
EXPIRES_RE = re.compile(r'Expires\s+([0-9]{1,2}\s+\w+\s+\d{4})', re.I)
LOGO_ALT_RE = re.compile(r'company-list-logo.*?<img[^>]*alt="([^"]*)"', re.S)
CONTENT_RE = re.compile(
  r'single-candidate-bottom-left">(.*?)(?:single-candidate-bottom-right|class="related|<footer)',
  re.S,
)
EXTERNAL_RE = re.compile(r'href="(https?://(?!(?:www\.)?ihararejobs\.com)[^"]+)"', re.I)
SOCIAL = ("facebook.", "twitter.", "linkedin.", "instagram.", "wa.me", "whatsapp", "youtube.", "t.me", "iharare.com")


def parse_cards(html: str) -> list[dict]:
  cards: list[dict] = []
  # Each card's logo block precedes its details; capture the alt as a company fallback.
  for match in CARD_RE.finditer(html):
    rest = match.group("rest")
    location = MAP_RE.search(rest)
    company = BUILDING_RE.search(rest)
    expires = EXPIRES_RE.search(rest)
    cards.append({
      "url": base.absolute_url(BASE_URL, match.group("href")),
      "role": base.strip_tags(match.group("title")),
      "company": base.strip_tags(company.group(1)) if company else "",
      "location": base.strip_tags(location.group(1)) if location else "",
      "closing": base.strip_tags(expires.group(1)) if expires else None,
    })
  return cards


def parse_detail(html: str) -> tuple[str, list[str], list[str]]:
  """Return (description, emails, external_apply_links)."""
  region = CONTENT_RE.search(html)
  scope = region.group(1) if region else html
  description = base.strip_tags(scope)
  emails = base.extract_emails(scope)
  links: list[str] = []
  for href in EXTERNAL_RE.findall(scope):
    if not any(token in href.lower() for token in SOCIAL):
      if href not in links:
        links.append(href)
  return description, emails, links[:2]


def listing_url(term: str, page: int) -> str:
  params: dict[str, object] = {}
  if term:
    params["search"] = term
  if page > 1:
    params["page"] = page
  return f"{LIST_URL}?{urllib.parse.urlencode(params)}" if params else LIST_URL


def fetch(
  limit: int = 15,
  timeout: int = base.DEFAULT_TIMEOUT,
  match=None,
  roles: list[str] | None = None,
) -> list[dict]:
  candidates: list[dict] = []
  fetched = 0
  seen: set[str] = set()
  for term in base.source_search_terms(roles or []):
    page = 1
    while len(candidates) < max(0, limit):
      html = base.http_get(listing_url(term, page), timeout=timeout)
      if html is None:
        raise RuntimeError(f"iHarare listing page {page} request failed")
      cards = parse_cards(html)
      if not cards:
        break
      last_page = base.pagination_max(html, page)
      for card in cards:
        if len(candidates) >= max(0, limit):
          break
        if card["url"] in seen:
          continue
        seen.add(card["url"])
        if match is not None and not match(card["role"], f"{card['role']} {card['location']}"):
          continue
        description = ""
        emails: list[str] = []
        apply_links: list[str] = []
        if fetched < base.MAX_DETAIL_FETCHES:
          detail = base.http_get(
            card["url"],
            timeout=min(timeout, base.DETAIL_TIMEOUT),
            attempts=1,
          )
          if detail:
            description, emails, apply_links = parse_detail(detail)
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
          apply_links=apply_links,
        ))
      if page >= last_page:
        break
      page += 1
  return candidates


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(prog="iharare")
  parser.add_argument("--limit", type=int, default=10)
  parser.add_argument("--query", help="optional case-insensitive role/keyword filter")
  args = parser.parse_args(argv)
  match = base.build_candidate_matcher([args.query]) if args.query else None
  return base.print_candidates(fetch(limit=args.limit, match=match, roles=[args.query] if args.query else None))


if __name__ == "__main__":
  raise SystemExit(main())
