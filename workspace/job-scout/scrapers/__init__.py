#!/usr/bin/env python3
"""Job board scrapers that extend (not replace) the JSearch source.

Each board module exposes `fetch(limit, timeout, match=None)` returning candidate
dicts in the exact shape of job_scout.normalized_candidate. fetch_scraped_candidates
runs all boards, role-filters at the listing stage (cheap, before detail fetches),
and returns a merged list ready for job_scout.filter_candidates.

Std-lib only. A single board failing must never abort the run.
"""

from __future__ import annotations

import sys
import time

from . import applynow, base, iharare, vacancybox, vacancymail, zimlinks

# Ordered registry: name -> module exposing fetch(limit, timeout, match).
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
  match = base.build_candidate_matcher(target_roles(preferences))
  pool = max(int(limit or 0), LISTING_POOL)
  candidates: list[dict] = []
  results: dict[str, dict] = {}
  for name, module in SCRAPERS.items():
    started = time.monotonic()
    try:
      fetched = module.fetch(limit=pool, timeout=timeout, match=match)
      candidates.extend(fetched)
      results[name] = {
        "status": "ok",
        "rawCount": len(fetched),
        "durationMs": round((time.monotonic() - started) * 1000),
      }
    except Exception as err:  # one board's failure must not sink the others
      print(f"[scrapers] {name} failed: {err!r}", file=sys.stderr)
      results[name] = {
        "status": "unavailable",
        "rawCount": 0,
        "error": str(err)[:180],
        "durationMs": round((time.monotonic() - started) * 1000),
      }
  return candidates, results


def fetch_scraped_candidates(preferences: dict, limit: int, timeout: int = base.DEFAULT_TIMEOUT) -> list[dict]:
  candidates, _ = fetch_scraped_candidates_with_results(preferences, limit, timeout)
  return candidates
