"""Subscriber-record accessors and per-subscriber CV/artifact paths.

Shape a ready subscriber into the fields dispatch needs: display/applicant name,
phone, sender email, phone-hash, the cached R2 CV download, and the on-disk
application artifact directory.
"""

from __future__ import annotations

from pathlib import Path
import re

from jobscout import api
from .constants import DEFAULT_STORAGE_ROOT
from .profile import subscriber_user_id
from .util import phone_hash, slug_part


def display_name(subscriber: dict) -> str:
  profile = subscriber.get("profile")
  profile = profile if isinstance(profile, dict) else {}
  for value in (
    profile.get("displayName"),
    profile.get("name"),
    subscriber.get("displayName"),
    subscriber.get("name"),
    subscriber.get("senderEmail"),
    profile.get("email"),
  ):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if text:
      return text.split("@", 1)[0]
  return "Applicant"


def document_filename(subscriber: dict, kind: str) -> str:
  """Applicant-named PDF filename, e.g. 'Joseph-Munemo-CV.pdf'.

  slug_part lowercases and hyphenates; applicant-facing filenames must keep the
  original case, so build a filesystem/email-safe slug inline here.
  """
  raw = re.sub(r"[^A-Za-z0-9._-]+", "-", display_name(subscriber).strip())
  slug = re.sub(r"-{2,}", "-", raw).strip("-.")[:80] or "Applicant"
  label = {"cv": "CV", "cover_letter": "Cover-Letter"}[kind]
  return f"{slug}-{label}.pdf"


def applicant_name(subscriber: dict) -> tuple[str, str]:
  parts = display_name(subscriber).split()
  if len(parts) <= 1:
    return (parts[0] if parts else "Applicant", "")
  return parts[0], " ".join(parts[1:])


def subscriber_phone(subscriber: dict) -> str:
  phone = str(subscriber.get("whatsappPhone") or "").strip()
  if not phone:
    raise RuntimeError("subscriber is missing whatsappPhone")
  return phone


def subscriber_sender_email(subscriber: dict) -> str:
  email = str(subscriber.get("senderEmail") or "").strip()
  if not email:
    raise RuntimeError("subscriber is missing senderEmail")
  return email


def subscriber_phone_hash(subscriber: dict) -> str:
  value = str(subscriber.get("whatsappPhoneHash") or "").strip()
  if value:
    return re.sub(r"[^A-Za-z0-9_.:-]", "-", value)[:80]
  return phone_hash(subscriber_phone(subscriber))


# cvFileRef/cvHtmlRef are R2 object keys, not local paths. Downloaded CVs are
# cached per user and object reference for the duration of a dispatch run so a
# replacement upload cannot accidentally reuse the previous document.
_cv_download_cache: dict[str, tuple[str, str]] = {}


def cv_local_dest(subscriber: dict) -> Path:
  ref = str(subscriber.get("cvHtmlRef") or subscriber.get("cvFileRef") or "").strip()
  suffix = ".html" if ref.lower().endswith((".html", ".htm")) else ".pdf"
  return DEFAULT_STORAGE_ROOT / subscriber_phone_hash(subscriber) / f"cv{suffix}"


def canonical_cv_ref(subscriber: dict) -> str:
  return str(subscriber.get("cvHtmlRef") or subscriber.get("cvFileRef") or "").strip()


def canonical_cv_cache_key(subscriber: dict) -> str:
  return "|".join([
    canonical_cv_ref(subscriber),
    str(subscriber.get("cvSourceVersion") or "").strip(),
    str(subscriber.get("cvConvertedAt") or "").strip(),
  ])


def subscriber_cv_html_ready(subscriber: dict) -> bool:
  """Gate-side twin of subscriber_cv_html's preconditions.

  Dispatch readiness must exclude pending/failed conversions and legacy
  PDF-only records up front instead of failing them downstream every run.
  """
  status = str(subscriber.get("cvConversionStatus") or "").strip().casefold()
  if status not in {"", "ready"}:
    return False
  return canonical_cv_ref(subscriber).lower().endswith((".html", ".htm"))


def subscriber_cv_html(subscriber: dict) -> str:
  """Return the locally cached ready canonical HTML CV.

  Pending/failed conversions and legacy PDF-only records must never reach the
  AI personalization or dispatch path.
  """
  status = str(subscriber.get("cvConversionStatus") or "").strip().casefold()
  if status and status != "ready":
    raise RuntimeError("subscriber CV conversion is not ready")
  ref = canonical_cv_ref(subscriber)
  if not ref:
    raise RuntimeError("subscriber is missing canonical CV HTML")
  if not ref.lower().endswith((".html", ".htm")):
    raise RuntimeError("subscriber canonical CV is not HTML")
  return subscriber_cv_file(subscriber)


def subscriber_cv_file(subscriber: dict) -> str:
  user_id = subscriber_user_id(subscriber)
  ref = canonical_cv_ref(subscriber)
  cache_key = canonical_cv_cache_key(subscriber)
  cached = _cv_download_cache.get(user_id)
  if cached and cached[0] == cache_key and Path(cached[1]).is_file():
    return cached[1]
  if not ref:
    raise RuntimeError("subscriber is missing cvFileRef")
  path = str(api.download_cv(user_id, cv_local_dest(subscriber)))
  _cv_download_cache[user_id] = (cache_key, path)
  return path


def application_artifact_dir(subscriber: dict, candidate: dict, attempt: int | None = None) -> Path:
  directory = (
    DEFAULT_STORAGE_ROOT.parent
    / "users"
    / subscriber_phone_hash(subscriber)
    / "applications"
    / slug_part(f"{candidate.get('company', '')}-{candidate.get('role', '')}")
  )
  return directory / str(attempt) if attempt is not None else directory
