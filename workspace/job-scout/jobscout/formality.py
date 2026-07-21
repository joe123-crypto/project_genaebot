"""Employer-formality classification for Job Scout.

Government, parastatal, and public-service employers in Zimbabwe expect a more
formal cover-letter register than an ordinary private business. This module
holds the single, keyword-based classifier so both the deterministic fallback
letter (``application.cover_letter_text``) and the AI-agent guidance packet
(``agent_dispatch``) raise the register from the same source of truth.

The classifier is deliberately conservative: a formal register on a private
employer only reads slightly stiff, but classifying a genuine ministry as
"standard" would under-serve it. We therefore require reasonably strong public
sector signals rather than any lone weak token. A bare "Corporation" is a
common private-company suffix, so it is NOT a signal on its own; a qualified
state pattern such as "Development Corporation" or "Mining Development
Corporation" is.
"""

from __future__ import annotations

import re

# Strong, largely unambiguous public-sector signals. Any single hit here is
# enough to classify "formal".
_FORMAL_SUBSTRINGS = (
  "ministry",
  "government",
  "public service",
  "parastatal",
  "state enterprise",
  "development corporation",
  "reserve bank",
  "city council",
  "rural district council",
  "town council",
  "provincial council",
  "municipality",
)

# Word-boundary patterns for state-entity name shapes. "Commission" and
# "Authority" are strong as standalone words (e.g. "Public Service Commission",
# "Zimbabwe Revenue Authority") — they are rare in private company names. A bare
# "Corporation"/"Council" is too common to trust alone and is handled only via
# the qualified substrings above.
_FORMAL_PATTERNS = tuple(
  re.compile(pattern, re.IGNORECASE)
  for pattern in (
    r"\bcommission\b",
    r"\bauthority\b",
    # "Zimbabwe <something> Corporation" is a classic state-enterprise shape.
    r"\bzimbabwe\b.*\bcorporation\b",
  )
)


def employer_formality(candidate: dict) -> str:
  """Classify an employer as "formal" (public-sector) or "standard".

  Case-insensitive keyword/pattern match over the candidate's company, role,
  and description. Conservative by design (see the module docstring): only
  reasonably strong government/public-sector signals return "formal".
  """
  parts = [
    str(candidate.get("company") or ""),
    str(candidate.get("role") or ""),
    str(candidate.get("description") or ""),
  ]
  haystack = " ".join(parts).casefold()
  if not haystack.strip():
    return "standard"
  if any(signal in haystack for signal in _FORMAL_SUBSTRINGS):
    return "formal"
  if any(pattern.search(haystack) for pattern in _FORMAL_PATTERNS):
    return "formal"
  return "standard"
