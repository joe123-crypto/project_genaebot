#!/usr/bin/env python3
"""Scraper for ApplyNOW Zimbabwe (https://applynow.co.zw/).

ApplyNOW is a WordPress/Foxiz site that has no static jobs feed (/jobs/ redirects
to a single post), but it exposes month archives at /YYYY/MM/ that list every
opportunity published that month. Each listing card looks like:

    <div class="p-wrap p-list p-list-2" data-pid="23185">
      ... <img ... alt="Textile Exchange" class="featured-img wp-post-image" ...> ...
      <h2 class="entry-title"> <a class="p-url" href="https://applynow.co.zw/2026/07/05/textile-exchange-2/" rel="bookmark"> Textile Exchange is hiring: ... </a></h2>
      ... <time class="updated" datetime="2026-07-05T02:21:31+02:00">13 hours ago</time> ...
    </div>

The detail page holds the full description inside .entry-content and may carry an
application e-mail and/or an external "apply" link. We walk back a few months from
today so a single run still surfaces recent postings. Std-lib only.
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
import time

if __package__ in (None, ""):
  sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__file__)))
  from scrapers import base
else:
  from . import base

SOURCE = "applynow"
BASE_URL = "https://applynow.co.zw"
# How many months of archives to walk back (current month + previous ones).
MONTH_LOOKBACK = 3

# Each listing card starts at a p-wrap block; split on it and parse each segment.
CARD_SPLIT_RE = re.compile(r'<div class="p-wrap ')
TITLE_RE = re.compile(
  r'<h2 class="entry-title">\s*<a class="p-url" href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
  re.S,
)
# Featured-image alt carries the organisation name; tolerate either attribute order.
ALT_BEFORE_RE = re.compile(r'<img[^>]*class="featured-img[^"]*"[^>]*alt="(?P<alt>[^"]*)"', re.S)
ALT_AFTER_RE = re.compile(r'<img[^>]*alt="(?P<alt>[^"]*)"[^>]*class="featured-img', re.S)
APPLY_BY_RE = re.compile(r'Apply by\s+([0-9]{1,2}\s+\w+\s+\d{4})', re.I)
CLOSING_RE = re.compile(r'Closing Date:?\s*([0-9]{1,2}\s+\w+\s+\d{4})', re.I)
# Detail page body lives in .entry-content; stop before the related/footer noise.
CONTENT_RE = re.compile(
  r'class="entry-content[^"]*"(.*?)'
  r'(?:class="entry-footer|class="single-related|class="rb-tags|class="p-share|class="author-box|<footer)',
  re.S,
)
EXTERNAL_RE = re.compile(r'href="(https?://(?!(?:www\.)?applynow\.co\.zw)[^"]+)"', re.I)
# Social handles + asset/CDN hosts that ride along on the page but never apply links.
SOCIAL = (
  "facebook.", "twitter.", "linkedin.", "instagram.", "youtube.", "wa.me",
  "whatsapp", "t.me", "fonts.googleapis", "fonts.gstatic", "gravatar",
  "foxiz.io", "cloudwaysapps", "w.org", "gmpg.org", "schema.org", "wp.me",
)


def _company(segment: str) -> str:
  alt = ALT_BEFORE_RE.search(segment) or ALT_AFTER_RE.search(segment)
  return base.strip_tags(alt.group("alt")) if alt else ""


def parse_cards(html: str) -> list[dict]:
  cards: list[dict] = []
  for segment in CARD_SPLIT_RE.split(html)[1:]:
    title = TITLE_RE.search(segment)
    if not title:
      continue
    role = base.strip_tags(title.group("title"))
    apply_by = APPLY_BY_RE.search(role)
    cards.append({
      "url": base.absolute_url(BASE_URL, title.group("href")),
      "role": role,
      "company": _company(segment),
      "closing": base.strip_tags(apply_by.group(1)) if apply_by else None,
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


def _months_back(count: int) -> list[tuple[int, int]]:
  """Return (year, month) pairs from this month back over `count` months."""
  today = datetime.date.today()
  year, month = today.year, today.month
  months: list[tuple[int, int]] = []
  for _ in range(max(1, count)):
    months.append((year, month))
    month -= 1
    if month == 0:
      month, year = 12, year - 1
  return months


def month_url(year: int, month: int) -> str:
  return f"{BASE_URL}/{year:04d}/{month:02d}/"


def fetch(limit: int = 15, timeout: int = base.DEFAULT_TIMEOUT, match=None) -> list[dict]:
  candidates: list[dict] = []
  fetched = 0
  seen: set[str] = set()
  for index, (year, month) in enumerate(_months_back(MONTH_LOOKBACK)):
    if len(candidates) >= max(0, limit):
      break
    html = base.http_get(month_url(year, month), timeout=timeout)
    if html is None:
      if index == 0:
        raise RuntimeError("ApplyNow listing request failed")
      continue
    if not html:
      continue
    for card in parse_cards(html):
      if len(candidates) >= max(0, limit):
        break
      if card["url"] in seen:
        continue
      seen.add(card["url"])
      if match is not None and not match(card["role"], f"{card['role']} {card['company']}"):
        continue
      description = ""
      emails: list[str] = []
      apply_links: list[str] = []
      closing = card["closing"]
      if fetched < base.MAX_DETAIL_FETCHES:
        detail = base.http_get(card["url"], timeout=timeout)
        if detail:
          description, emails, apply_links = parse_detail(detail)
          if not closing:
            body_closing = CLOSING_RE.search(detail)
            if body_closing:
              closing = base.strip_tags(body_closing.group(1))
        fetched += 1
        time.sleep(base.RATE_LIMIT_SECONDS)
      candidates.append(base.build_candidate(
        SOURCE,
        role=card["role"],
        company=card["company"],
        url=card["url"],
        description=description,
        location="",
        closing=closing,
        emails=emails,
        apply_links=apply_links,
      ))
  return candidates


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(prog="applynow")
  parser.add_argument("--limit", type=int, default=10)
  parser.add_argument("--query", help="optional case-insensitive role/keyword filter")
  args = parser.parse_args(argv)
  match = base.keyword_match(args.query) if args.query else None
  return base.print_candidates(fetch(limit=args.limit, match=match))


if __name__ == "__main__":
  raise SystemExit(main())
