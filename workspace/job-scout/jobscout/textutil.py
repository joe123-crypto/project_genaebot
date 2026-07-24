"""Text sanitization helpers shared across Job Scout modules."""

from __future__ import annotations

import re

ROLE_SPLIT_RE = re.compile(r"[.\n|;•]")
UNKNOWN_EMPLOYER_TEXT = "unknown employer"
UNKNOWN_ROLE_TEXT = "unknown role"
BAD_ROLE_START_RE = re.compile(
  r"^(?:motivated|detail-oriented|detail oriented|enthusiastic|young|innovative|hardworking|reliable|eager|highly|suitably|qualified|experienced|skilled|dynamic|friendly|organized|organised|proactive)\b",
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
TITLE_PREFIX_RE = re.compile(
  r"^(?:re[-\s]?advertisement|advertisement|job\s+advert|job\s+ad|job\s+vacancy|vacancy\s+announcement|vacancy\s+alert|vacancy|career\s+opportunity|call\s+for|job\s+title|position)\b\s*[:\-–—]*\s*",
  re.I,
)
TITLE_BOUNDARY_RE = re.compile(
  r"\b(?:applicants?\s+who|are\s+kindly|a\s+growing\s+company|a\s+reputable\s+company|an?\s+[a-z-]+\s+company|our\s+client|we\s+are|we['’]re|are\s+you|is\s+seeking|are\s+seeking|seeks|looking\s+for|location\s*:|salary\b|expires?\b|job\s+description|requirements?\b|key\s+responsibilities|duties\b|about\s+us|apply\s+here|benefits?\b)\b",
  re.I,
)
TRAILING_OPEN_RE = re.compile(r"\s+(?:are|is)\s+now\s+open\b.*$", re.I)
TRAILING_YEAR_RE = re.compile(r"\b20\d{2}\b")
GENERIC_TITLE_RE = re.compile(
  r"^(?:job|jobs|vacanc(?:y|ies)|position(?:s)?|opportunit(?:y|ies)|career\s+opportunit(?:y|ies)|open\s+positions?|new\s+vacanc(?:y|ies))$",
  re.I,
)
MULTI_ROLE_RE = re.compile(
  r"\b(?:multiple|various|several|\d+)\s+(?:roles|positions|vacanc(?:y|ies)|opportunities)|\b(?:open\s+positions?|positions?\s+available|career\s+opportunities|new\s+vacancies|following\s+(?:mentioned\s+)?positions|we\s+have\s+the\s+following)\b",
  re.I,
)
ROLE_NOUN_RE = re.compile(
  r"\b(?:accountant|administrator|advisor|analyst|assistant|attachment|attendant|auditor|bookkeeper|builder|buyer|cashier|charge|chef|cleaner|clerk|commissionaire|consultant|controller|coordinator|cook|developer|doctor|driver|electrician|engineer|estimator|fellowships?|filler|graduate|guard|hand|inspector|intern(?:ship)?|leader|manager|marketer|mechanic|nurse|officer|operator|orderly|painter|porter|presenter|plumber|receptionist|representative|sales(?:man|person)?|secretary|specialist|students?|supervisor|surveyor|technician|trainee|waiter|waitress|welder)s?\b",
  re.I,
)
ADJECTIVE_TITLE_RE = re.compile(
  r"\b(?:motivated|detail-oriented|detail oriented|enthusiastic|young|innovative|hardworking|reliable|eager|highly|suitably|qualified|experienced|skilled|dynamic|friendly|organized|organised|professional|proactive|talented|disciplined|ambitious|honest|competent|safety-conscious|tech-savvy|high-caliber|self-motivated|results-driven)(?:[\s,/-]+(?:and\s+)?(?:motivated|detail-oriented|detail oriented|enthusiastic|young|innovative|hardworking|reliable|eager|highly|suitably|qualified|experienced|skilled|dynamic|friendly|organized|organised|professional|proactive|talented|disciplined|ambitious|honest|competent|safety-conscious|tech-savvy|high-caliber|self-motivated|results-driven))*\s+(?P<title>[A-Z][A-Za-z0-9&/()' -]{2,70}?)(?:\s+to\b|\s+who\b|\s+where\b|$)",
  re.I,
)
POSITION_OF_RE = re.compile(
  r"\b(?:position|role)\s+of\s+(?P<title>[A-Z][A-Za-z0-9&/()' -]{2,70})",
  re.I,
)
HIRING_PHRASE_RE = re.compile(
  r"\b(?:we\s+are|we['’]re|is|are)\s+hiring\s+(?P<title>[^.!?\n|•]{3,90})",
  re.I,
)
SEEKING_PHRASE_RE = re.compile(
  r"\b(?:seeking|looking\s+for|recruiting)\s+(?:an?\s+|the\s+)?(?P<title>[^.!?\n|•]{3,90})",
  re.I,
)
QUALIFICATION_TITLE_RE = re.compile(
  r"\b(?P<title>[A-Z][A-Za-z0-9&/()' -]{2,70})\s+Qualifications?\b",
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


def _plain_text(value: object, max_len: int = 1000) -> str:
  text = re.sub(r"<[^>]+>", " ", str(value or ""))
  text = re.sub(r"https?://\S+", " ", text)
  text = re.sub(r"[\r\t]+", " ", text)
  text = re.sub(r"[*_`#]+", "", text)
  text = re.sub(r"[📢🌍📍✅⚡🚛🟢]+", " ", text)
  text = re.sub(r"[ \f\v]+", " ", text)
  return text.strip()[:max_len]


def _title_word(value: str) -> str:
  if value.isupper() and len(value) <= 3:
    return value
  if value.isupper() and any(ch.isalpha() for ch in value):
    return value[:1].upper() + value[1:].lower()
  return value[:1].upper() + value[1:]


def _smart_title(value: str) -> str:
  words: list[str] = []
  for word in value.split():
    words.append("-".join(_title_word(part) for part in word.split("-")))
  text = " ".join(words)
  return re.sub(r"\(([a-z]{2,4})\)", lambda match: f"({match.group(1).upper()})", text)


def _company_text(candidate: dict) -> str:
  company = natural_email_text(candidate.get("company"), 120)
  if company.casefold() == UNKNOWN_EMPLOYER_TEXT:
    return ""
  return company


def _multiple_roles_title(candidate: dict, text: str) -> str:
  if not MULTI_ROLE_RE.search(text) and len(re.findall(r"\bx\s*\d+\b", text, re.I)) < 2:
    return ""
  company = _company_text(candidate)
  return f"Multiple roles at {company}" if company else "Multiple roles"


def _strip_company_prefix(text: str, company: str) -> str:
  if not company:
    return text
  escaped = re.escape(company)
  return re.sub(rf"^\s*{escaped}\b\s*[-:|–—]?\s*", "", text, flags=re.I).strip()


def _cut_title_boundaries(text: str) -> str:
  text = TRAILING_OPEN_RE.sub("", text)
  matches = [
    match
    for match in (
      TITLE_BOUNDARY_RE.search(text),
      re.search(r"\b(?:company|location|host|category|position\s+summary)\s*:", text, re.I),
    )
    if match and match.start() > 2
  ]
  if matches:
    text = text[:min(match.start() for match in matches)]
  if re.search(r"\bexpires?\b", text, re.I):
    text = re.split(r"\s*[-–—]\s*", text, maxsplit=1)[0]
  text = re.split(r"\s+[|•]\s+", text, maxsplit=1)[0]
  text = re.sub(r"\([^)]*\bcompany\b[^)]*\)", " ", text, flags=re.I)
  text = TRAILING_YEAR_RE.sub("", text)
  return text.strip(" -:.,;![]")


def _clean_recovered_title(value: object, *, allow_lowercase: bool = False) -> str:
  raw = _plain_text(value, 240)
  if not raw or raw.casefold() == UNKNOWN_ROLE_TEXT:
    return ""
  if re.fullmatch(r"(?:www\.)?\S+\.\w{2,}(?:/\S*)?", raw, re.I) or "@" in raw:
    return ""
  raw = TITLE_PREFIX_RE.sub("", raw).strip()
  raw = _cut_title_boundaries(raw)
  if " - " in raw:
    left, right = raw.split(" - ", 1)
    if ROLE_NOUN_RE.search(left) and (" - " in right or len(raw.split()) > 9):
      raw = left
  raw = re.sub(r"\s+", " ", raw).strip(" -:.,;![]")
  if not raw or GENERIC_TITLE_RE.fullmatch(raw):
    return ""
  if re.search(r"\b(?:jobs|vacanc(?:y|ies)|opportunit(?:y|ies)|positions?)\b", raw, re.I) and not ROLE_NOUN_RE.search(raw):
    return ""
  if not ROLE_NOUN_RE.search(raw):
    return ""

  title_source = _smart_title(raw) if raw.isupper() else raw
  cleaned = clean_job_title(title_source)
  if cleaned:
    return cleaned
  if not allow_lowercase and raw[:1].islower():
    return ""
  if BAD_ROLE_START_RE.search(raw):
    return ""
  if len(raw) > 70 or len(raw.split()) > 9:
    return ""
  if not ROLE_NOUN_RE.search(raw):
    return ""
  return natural_email_text(_smart_title(raw), 120)


def _credible_clean_title(value: str) -> str:
  title = natural_email_text(_smart_title(value), 120) if value.isupper() else natural_email_text(value, 120)
  if not title or GENERIC_TITLE_RE.fullmatch(title) or not ROLE_NOUN_RE.search(title):
    return ""
  return title


def _recover_title_from_fragment(value: object) -> str:
  text = _plain_text(value, 500)
  if not text:
    return ""
  for pattern in (ADJECTIVE_TITLE_RE, POSITION_OF_RE):
    match = pattern.search(text)
    if match:
      title = _clean_recovered_title(match.group("title"), allow_lowercase=False)
      if title:
        return title
  match = re.match(r"(?P<title>[A-Za-z][A-Za-z0-9&/()' -]{2,70}?)\s+to\b", text)
  if match:
    title = _clean_recovered_title(match.group("title"), allow_lowercase=False)
    if title:
      return title
  return _clean_recovered_title(text, allow_lowercase=True)


def _description_title_candidates(text: str, company: str) -> list[str]:
  normalized = _plain_text(text, 1400).replace("\n", "\n")
  lines = [
    re.sub(r"\s+", " ", line).strip()
    for line in re.split(r"\n+", normalized)
    if line.strip()
  ]
  if not lines and normalized:
    lines = [normalized]
  candidates: list[str] = []
  for line in lines[:8]:
    line = _strip_company_prefix(line, company)
    candidates.append(line)
    candidates.append(TITLE_PREFIX_RE.sub("", line).strip())
  first = _strip_company_prefix(normalized, company)
  for pattern in (POSITION_OF_RE, HIRING_PHRASE_RE, SEEKING_PHRASE_RE, QUALIFICATION_TITLE_RE):
    match = pattern.search(first[:800])
    if match:
      title = match.group("title")
      if not title.strip().casefold().startswith("at "):
        candidates.append(title)
  return candidates


def display_job_title(candidate: dict) -> str | None:
  """Professional candidate title for user-visible text, or None if none is credible."""
  for key in ("role", "title", "jobTitle", "job_title"):
    title = clean_job_title(candidate.get(key))
    title = _credible_clean_title(title)
    if title:
      return title
  for key in ("role", "title", "jobTitle", "job_title"):
    title = _recover_title_from_fragment(candidate.get(key))
    if title:
      return title

  description = _plain_text(candidate.get("description"), 1400)
  multi = _multiple_roles_title(candidate, description)
  company = _company_text(candidate)
  for value in _description_title_candidates(str(candidate.get("description") or ""), company):
    title = _clean_recovered_title(value, allow_lowercase=False)
    if title:
      return title
  return multi or None
