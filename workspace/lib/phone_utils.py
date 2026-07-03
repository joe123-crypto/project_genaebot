"""Shared phone normalization and hashing for subscription registries.

Extracted verbatim from the public-events / public-news registry scripts, which
used byte-identical copies. Other workspace scripts (account-link, job-scout,
whatsapp-invite/public-user) intentionally keep their own variants: they raise
different exception types and use different length bounds, so they are not
interchangeable with these.
"""

from __future__ import annotations

import hashlib
import re


def normalize_phone(value: str) -> str:
    value = str(value or "").strip()
    digits = re.sub(r"\D", "", value)
    if not digits:
        raise ValueError("phone is required")
    normalized = f"+{digits}"
    if len(normalized) < 9 or len(normalized) > 16:
        raise ValueError("phone must normalize to 9-16 digits")
    return normalized


def phone_hash(phone: str) -> str:
    return hashlib.sha256(f"whatsapp:{phone}".encode("utf-8")).hexdigest()[:12]
