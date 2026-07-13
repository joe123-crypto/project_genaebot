"""Text sanitization helpers shared across Job Scout modules."""

from __future__ import annotations

import re

ROLE_SPLIT_RE = re.compile(r"[.\n|;•]")
BAD_ROLE_START_RE = re.compile(
  r"^(?:motivated|detail-oriented|detail oriented|enthusiastic|young|innovative|hardworking|reliable|eager|highly|suitably|qualified|experienced)\b",
  re.I,
)
HIRING_PREFIX_RE = re.compile(
  r"^.+?\b(?:is\s+)?hiring\s*:?\s*(?:for\s+)?(?:(?:a|an|the)\s+)?(?P<role>.+)$",
  re.I,
)
TRAILING_ROLE_META_RE = re.compile(
  r"\s+(?:-|--|–|—)\s*(?:closing\s+date|apply\s+by|deadline|closes|expires)\b.*$",
  re.I,
)
TRAILING_RECRUITING_CLAUSE_RE = re.compile(
  r"\s+to\s+join\s+(?:(?:them|us)\b|(?:(?:our|their|the|a|an)\s+)?(?:team|company|organisation|organization|department|division|group|workforce)\b).*$",
  re.I,
)


def public_text(value: object, max_len: int = 180) -> str:
  text = re.sub(r"[\r\n\t]+", " ", str(value or ""))
  text = re.sub(r"\s+", " ", text).strip()
  text = re.sub(r"/home/joseph/\S+", "[local path]", text)
  text = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text, flags=re.I)
  text = re.sub(r"([A-Za-z0-9._%+-]{2})[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", r"\1***\2", text)
  return text[:max_len]


def natural_email_text(value: object, max_len: int = 180) -> str:
  text = public_text(value, max_len)
  text = text.replace("—", " - ").replace("–", " - ")
  text = re.sub(r"\s+", " ", text).strip()
  return text[:max_len]


def clean_job_title(value: object) -> str:
  text = public_text(value, 240)
  text = re.sub(r"[*_`]+", "", text).strip(" -:.,")
  text = TRAILING_ROLE_META_RE.sub("", text).strip(" -:.,")
  text = ROLE_SPLIT_RE.split(text, maxsplit=1)[0].strip(" -:.,")

  hiring = HIRING_PREFIX_RE.match(text)
  if hiring:
    text = hiring.group("role").strip(" -:.,")

  text = TRAILING_ROLE_META_RE.sub("", text).strip(" -:.,")
  text = TRAILING_RECRUITING_CLAUSE_RE.sub("", text).strip(" -:.,")
  text = ROLE_SPLIT_RE.split(text, maxsplit=1)[0].strip(" -:.,")
  if not text or text.casefold() == "unknown role":
    return ""
  if BAD_ROLE_START_RE.search(text):
    return ""
  if len(text) > 70 or len(text.split()) > 9:
    return ""
  if text[0].islower():
    return ""
  return natural_email_text(text, 120)
