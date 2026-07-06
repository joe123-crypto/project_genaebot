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


# cvFileRef is now an R2 object key, not a local path. Downloaded CVs are cached
# per user for the duration of a dispatch run so multiple candidates reuse one
# download.
_cv_download_cache: dict[str, str] = {}


def cv_local_dest(subscriber: dict) -> Path:
  return DEFAULT_STORAGE_ROOT / subscriber_phone_hash(subscriber) / "cv.pdf"


def subscriber_cv_file(subscriber: dict) -> str:
  user_id = subscriber_user_id(subscriber)
  cached = _cv_download_cache.get(user_id)
  if cached and Path(cached).is_file():
    return cached
  if not str(subscriber.get("cvFileRef") or "").strip():
    raise RuntimeError("subscriber is missing cvFileRef")
  path = str(api.download_cv(user_id, cv_local_dest(subscriber)))
  _cv_download_cache[user_id] = path
  return path


def application_artifact_dir(subscriber: dict, candidate: dict) -> Path:
  return (
    DEFAULT_STORAGE_ROOT.parent
    / "users"
    / subscriber_phone_hash(subscriber)
    / "applications"
    / slug_part(f"{candidate.get('company', '')}-{candidate.get('role', '')}")
  )
