"""Small shared helpers for the CV maker.

Settings lookup (process environment, then OpenClaw's .env), the Agent Genaie
internal endpoint and key, phone normalization, timestamps, the customer-safe
error text scrubber, the OpenClaw CLI location, and the applicant-named PDF
filename. No network side effects.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil


PRODUCTION_OPENCLAW_ROOT = Path("/home/joseph/.openclaw")
DEFAULT_AGENT_BASE_URL = "http://localhost:3000"
DEFAULT_ENV_PATH = Path(os.environ.get("OPENCLAW_ENV_PATH", str(PRODUCTION_OPENCLAW_ROOT / ".env"))).expanduser()
OPENCLAW_BIN_FALLBACKS = (
  Path("/usr/local/bin/openclaw"),
  Path("/home/joseph/.npm-global/bin/openclaw"),
)
# An Agent Genaie mail URL ends in one of these; the base URL is what precedes it.
_MAIL_SUFFIXES = ("/internal/gmail/send", "/internal/gmail/senders")


def parse_env_file(path: Path) -> dict[str, str]:
  values: dict[str, str] = {}
  if not path.exists():
    return values
  for raw_line in path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
      continue
    if line.startswith("export "):
      line = line[len("export ") :].strip()
    if "=" not in line:
      continue
    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and ((value[0] == value[-1] == '"') or (value[0] == value[-1] == "'")):
      value = value[1:-1]
    if key:
      values[key] = value
  return values


def env_default(name: str, fallback: str = "") -> str:
  """Resolve a setting from the process environment, then OpenClaw's .env."""
  if os.environ.get(name):
    return os.environ[name]
  return parse_env_file(DEFAULT_ENV_PATH).get(name, fallback)


def agent_base_url() -> str:
  if env_default("AGENT_GENAI_BASE_URL"):
    return env_default("AGENT_GENAI_BASE_URL").rstrip("/")
  if env_default("APP_BASE_URL"):
    return env_default("APP_BASE_URL").rstrip("/")
  mail_url = env_default("AGENT_GENAI_MAIL_URL", "")
  for suffix in _MAIL_SUFFIXES:
    if mail_url.endswith(suffix):
      return mail_url[: -len(suffix)].rstrip("/")
  return DEFAULT_AGENT_BASE_URL


def endpoint(path: str) -> str:
  return f"{agent_base_url()}{path}"


def api_key() -> str:
  return env_default("AGENT_GENAI_INTERNAL_API_KEY", "")


def resolve_openclaw_bin(fallbacks: tuple[Path, ...] = OPENCLAW_BIN_FALLBACKS) -> Path:
  configured = os.environ.get("OPENCLAW_BIN", "").strip()
  if configured:
    return Path(configured)
  discovered = shutil.which("openclaw")
  if discovered:
    return Path(discovered)
  for candidate in fallbacks:
    if candidate.is_file() and os.access(candidate, os.X_OK):
      return candidate
  return Path("openclaw")


OPENCLAW_BIN = resolve_openclaw_bin()


def normalize_phone(value: str) -> str:
  digits = re.sub(r"\D", "", str(value or ""))
  if not digits:
    raise SystemExit("phone is required")
  phone = f"+{digits}"
  if len(phone) < 9 or len(phone) > 16:
    raise SystemExit("phone must normalize to 9-16 digits")
  return phone


def utc_now() -> str:
  return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def public_text(value: object, max_len: int = 180) -> str:
  text = re.sub(r"[\r\n\t]+", " ", str(value or ""))
  text = re.sub(r"\s+", " ", text).strip()
  text = re.sub(r"/home/joseph/\S+", "[local path]", text)
  text = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text, flags=re.I)
  text = re.sub(r"([A-Za-z0-9._%+-]{2})[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", r"\1***\2", text)
  return text[:max_len]


def _display_name(record: dict) -> str:
  profile = record.get("profile")
  profile = profile if isinstance(profile, dict) else {}
  for value in (
    profile.get("displayName"),
    profile.get("name"),
    record.get("displayName"),
    record.get("name"),
    record.get("senderEmail"),
    profile.get("email"),
  ):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if text:
      return text.split("@", 1)[0]
  return "Applicant"


def document_filename(record: dict, kind: str) -> str:
  """Applicant-named PDF filename, e.g. 'Joseph-Munemo-CV.pdf'.

  Keeps the original case and builds a filesystem/WhatsApp-safe slug.
  """
  raw = re.sub(r"[^A-Za-z0-9._-]+", "-", _display_name(record).strip())
  slug = re.sub(r"-{2,}", "-", raw).strip("-.")[:80] or "Applicant"
  label = {"cv": "CV", "cover_letter": "Cover-Letter"}[kind]
  return f"{slug}-{label}.pdf"
