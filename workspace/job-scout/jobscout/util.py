"""Small pure helpers shared across Job Scout modules.

Phone normalization, identity hashing, timestamps, run-id sanitization, and
slugging. No network or filesystem side effects; safe for any sibling module to
import.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
import pwd
import re


def normalize_phone(value: str) -> str:
  digits = re.sub(r"\D", "", str(value or ""))
  if not digits:
    raise SystemExit("phone is required")
  phone = f"+{digits}"
  if len(phone) < 9 or len(phone) > 16:
    raise SystemExit("phone must normalize to 9-16 digits")
  return phone


def phone_hash(phone: str) -> str:
  return hashlib.sha256(f"whatsapp:{normalize_phone(phone)}".encode("utf-8")).hexdigest()[:12]


def utc_now() -> str:
  return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_run_id(value: str) -> str:
  text = re.sub(r"[^A-Za-z0-9_.:-]", "-", str(value or "").strip())
  text = text.strip(".-_:")
  if not text:
    raise SystemExit("run-id is required")
  if len(text) > 160:
    raise SystemExit("run-id is too long")
  return text


def effective_user() -> str:
  try:
    return pwd.getpwuid(os.geteuid()).pw_name
  except KeyError:
    return str(os.geteuid())


def count_arg(value: int, name: str) -> int:
  if value < 0:
    raise SystemExit(f"{name} must be >= 0")
  return value


def slug_part(value: object) -> str:
  text = re.sub(r"[^A-Za-z0-9]+", "-", str(value or "").lower())
  text = text.strip("-")
  return text[:80] or "job"
