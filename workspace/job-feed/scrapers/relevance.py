#!/usr/bin/env python3
"""Role/relevance matching for the job feed's board scrapers.

Turns configured target roles into board search terms and into predicates that
decide whether a listing is relevant, including the guarded hospitality
"waitstaff family" expansion. Pure over the role strings plus base.collapse_ws;
base re-exports these names so callers keep using base.<name>.
"""

from __future__ import annotations

import re

from . import base


WAITSTAFF_TRIGGERS = {"waitress", "waiter", "wait staff", "waitstaff"}
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
  collapsed = base.collapse_ws(phrase).casefold()
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
  normalized = {base.collapse_ws(role).casefold() for role in (roles or [])}
  return bool(normalized & WAITSTAFF_TRIGGERS)


def source_search_terms(roles: list[str]) -> list[str]:
  """Return bounded server-side board queries while preserving local role filtering."""
  normalized = [base.collapse_ws(role) for role in (roles or []) if base.collapse_ws(role)]
  if not normalized or any(role.casefold() in ANY_ROLE_TRIGGERS for role in normalized):
    return [""]

  output: list[str] = []
  seen: set[str] = set()
  waitstaff_added = False
  for role in normalized:
    if role.casefold() in WAITSTAFF_TRIGGERS:
      if not waitstaff_added:
        output.extend(["waitress", "waiter"])
        seen.update({"waitress", "waiter"})
        waitstaff_added = True
      continue
    key = role.casefold()
    if key not in seen:
      output.append(role)
      seen.add(key)
  return output or [""]


def build_candidate_matcher(roles: list[str]):
  """Match configured roles, including a guarded hospitality waitstaff family."""
  normalized_roles = {base.collapse_ws(role).casefold() for role in (roles or [])}
  if normalized_roles & ANY_ROLE_TRIGGERS:
    return lambda role, text: True
  ordinary = [role for role in (roles or []) if base.collapse_ws(role).casefold() not in WAITSTAFF_TRIGGERS]
  ordinary_match = build_role_matcher(ordinary) if ordinary else None
  waitstaff = uses_waitstaff_family(roles)

  def match(role: str, text: str) -> bool:
    if ordinary_match is not None and ordinary_match(role, text):
      return True
    if not waitstaff:
      return ordinary_match is None
    title = base.collapse_ws(role).casefold()
    context = f"{title} {base.collapse_ws(text).casefold()}"
    if any(phrase in title for phrase in WAITSTAFF_TITLE_PHRASES):
      return True
    if re.search(r"\b(?:server|staff)\b", title) and any(term in context for term in WAITSTAFF_CONTEXT):
      return True
    return False

  return match


def keyword_match(query: str):
  """Single-keyword/role predicate for standalone CLI use."""
  return build_role_matcher([query])
