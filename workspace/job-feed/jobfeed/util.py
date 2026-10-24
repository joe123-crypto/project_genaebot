"""Small pure helpers: timestamps and run-id sanitization."""

from __future__ import annotations

from datetime import datetime, timezone
import re


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
