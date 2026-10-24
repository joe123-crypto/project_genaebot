#!/usr/bin/env python3
"""Job board scrapers that feed the daily job-advert feed.

Each board module exposes `fetch(limit, timeout, match=None, roles=None)` returning candidate
dicts built by base.build_candidate. fetch_scraped_candidates runs all boards,
role-filters at the listing stage (cheap, before detail fetches), and returns a
merged list.

Std-lib only. A single board failing must never abort the run.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sys
import time

from . import applynow, base, iharare, relevance, vacancybox, vacancymail

# Ordered registry: name -> module exposing fetch(limit, timeout, match, roles).
SCRAPERS = {
  "vacancymail": vacancymail,
  "iharare": iharare,
  "vacancybox": vacancybox,
  "applynow": applynow,
}

# How many listings to pull per board before role filtering / the final cap.
LISTING_POOL = 20


def target_roles(preferences: dict) -> list[str]:
  roles = preferences.get("targetRoles")
  if not roles:
    roles = preferences.get("roles")
  if isinstance(roles, str):
    return [roles]
  return [str(r) for r in roles] if isinstance(roles, list) else []


def matches_roles(candidate: dict, preferences: dict) -> bool:
  """True when the candidate's role/description matches any target role (or none set)."""
  match = base.build_candidate_matcher(target_roles(preferences))
  return match(str(candidate.get("role") or ""), str(candidate.get("description") or ""))


def fetch_scraped_candidates_with_results(
  preferences: dict,
  limit: int,
  timeout: int = base.DEFAULT_TIMEOUT,
) -> tuple[list[dict], dict[str, dict]]:
  roles = target_roles(preferences)
  match = base.build_candidate_matcher(roles)
  pool = max(int(limit or 0), LISTING_POOL)
  candidates: list[dict] = []
  results: dict[str, dict] = {}

  def fetch_one(module) -> tuple[list[dict], dict, Exception | None]:
    started = time.monotonic()
    try:
      fetched = module.fetch(limit=pool, timeout=timeout, match=match, roles=roles)
      return fetched, {
        "status": "ok",
        "rawCount": len(fetched),
        "durationMs": round((time.monotonic() - started) * 1000),
      }, None
    except Exception as err:  # one board's failure must not sink the others
      return [], {
        "status": "unavailable",
        "rawCount": 0,
        "error": str(err)[:180],
        "durationMs": round((time.monotonic() - started) * 1000),
      }, err

  with ThreadPoolExecutor(max_workers=len(SCRAPERS), thread_name_prefix="job-feed-board") as executor:
    futures = {name: executor.submit(fetch_one, module) for name, module in SCRAPERS.items()}
    for name in SCRAPERS:
      fetched, result, error = futures[name].result()
      candidates.extend(fetched)
      results[name] = result
      if error is not None:
        print(f"[scrapers] {name} failed: {error!r}", file=sys.stderr)
  return candidates, results


def fetch_scraped_candidates(preferences: dict, limit: int, timeout: int = base.DEFAULT_TIMEOUT) -> list[dict]:
  candidates, _ = fetch_scraped_candidates_with_results(preferences, limit, timeout)
  return candidates
