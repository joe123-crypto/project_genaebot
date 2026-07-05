#!/usr/bin/env python3
"""Scraper for VacancyBox's public WP Job Manager listing feed."""

from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import sys
import time

if __package__ in (None, ""):
  sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__file__)))
  from scrapers import base
else:
  from . import base

SOURCE = "vacancybox"
LIST_URL = "https://vacancybox.co.zw/jm-ajax/get_listings/"

CARD_RE = re.compile(
  r'<li[^>]*class="[^"]*\bjob_listing\b[^"]*"[^>]*>(?P<body>.*?)(?=<li[^>]*class="[^"]*\bjob_listing\b|$)',
  re.S | re.I,
)
LINK_RE = re.compile(r'<a[^>]*href="([^"]+)"', re.I)
TITLE_RE = re.compile(r'<div[^>]*class="position"[^>]*>.*?<h3>(.*?)</h3>', re.S | re.I)
COMPANY_RE = re.compile(r'<div[^>]*class="company"[^>]*>.*?<strong>(.*?)</strong>', re.S | re.I)
LOCATION_RE = re.compile(r'<div[^>]*class="location"[^>]*>(.*?)</div>', re.S | re.I)
JSON_LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)
DUE_RE = re.compile(r'(?:DUE|CLOSING DATE|DEADLINE)\s*:\s*([0-9]{1,2}\s+\w+\s+\d{4})', re.I)


def parse_cards(html: str) -> list[dict]:
  cards: list[dict] = []
  for match in CARD_RE.finditer(html):
    body = match.group("body")
    link = LINK_RE.search(body)
    title = TITLE_RE.search(body)
    if not link or not title:
      continue
    company = COMPANY_RE.search(body)
    location = LOCATION_RE.search(body)
    cards.append({
      "url": base.absolute_url("https://vacancybox.co.zw", link.group(1)),
      "role": base.strip_tags(title.group(1)),
      "company": base.strip_tags(company.group(1)) if company else "",
      "location": base.strip_tags(location.group(1)) if location else "",
    })
  return cards


def job_posting_json(html: str) -> dict:
  postings: list[dict] = []
  for raw in JSON_LD_RE.findall(html):
    try:
      value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
      continue
    values = value.get("@graph", []) if isinstance(value, dict) and isinstance(value.get("@graph"), list) else [value]
    for item in values:
      if isinstance(item, dict) and item.get("@type") == "JobPosting":
        postings.append(item)
  return max(postings, key=lambda item: len(str(item.get("description") or "")), default={})


def posting_location(posting: dict) -> str:
  location = posting.get("jobLocation")
  if isinstance(location, list):
    location = location[0] if location else None
  if isinstance(location, dict):
    address = location.get("address")
    if isinstance(address, dict):
      return base.collapse_ws(", ".join(str(address.get(key) or "") for key in ("addressLocality", "addressRegion", "addressCountry")).strip(", "))
    return base.collapse_ws(address)
  return ""


def parse_detail(html: str) -> dict:
  posting = job_posting_json(html)
  raw_description = str(posting.get("description") or "")
  description = base.strip_tags(html_lib.unescape(raw_description))
  organization = posting.get("hiringOrganization")
  company = base.collapse_ws(organization.get("name")) if isinstance(organization, dict) else ""
  due = DUE_RE.search(description)
  return {
    "company": company,
    "location": posting_location(posting),
    "description": description,
    "emails": base.extract_emails(description),
    "closing": str(posting.get("validThrough") or (due.group(1) if due else "")).strip() or None,
  }


def candidate_from_card(card: dict, *, timeout: int, fetch_detail: bool) -> dict:
  detail_data: dict = {}
  if fetch_detail and card.get("url"):
    detail = base.http_get(str(card["url"]), timeout=timeout)
    if detail:
      detail_data = parse_detail(detail)
    time.sleep(base.RATE_LIMIT_SECONDS)
  return base.build_candidate(
    SOURCE,
    role=str(card.get("role") or ""),
    company=str(detail_data.get("company") or card.get("company") or ""),
    url=str(card.get("url") or ""),
    description=str(detail_data.get("description") or ""),
    location=str(detail_data.get("location") or card.get("location") or ""),
    closing=detail_data.get("closing"),
    emails=detail_data.get("emails") if isinstance(detail_data.get("emails"), list) else [],
  )


def fetch(limit: int = 15, timeout: int = base.DEFAULT_TIMEOUT, match=None) -> list[dict]:
  per_page = max(1, min(max(limit * 3, limit), 50))
  response = base.http_post_form_json(LIST_URL, {
    "search_keywords": "",
    "search_location": "",
    "filter_job_type": "",
    "per_page": per_page,
    "orderby": "date",
    "order": "DESC",
    "page": 1,
    "show_pagination": "true",
  }, timeout=timeout)
  if not isinstance(response, dict) or not isinstance(response.get("html"), str):
    raise RuntimeError("VacancyBox listing request failed")
  candidates: list[dict] = []
  fetched = 0
  for card in parse_cards(response["html"]):
    if len(candidates) >= max(0, limit):
      break
    if match is not None and not match(str(card.get("role") or ""), str(card.get("role") or "")):
      continue
    candidates.append(candidate_from_card(
      card,
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
