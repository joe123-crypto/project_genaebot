"""Text sanitization helpers shared across Job Scout modules."""

from __future__ import annotations

import re


def public_text(value: object, max_len: int = 180) -> str:
  text = re.sub(r"[\r\n\t]+", " ", str(value or ""))
  text = re.sub(r"\s+", " ", text).strip()
  text = re.sub(r"/home/joseph/\S+", "[local path]", text)
  text = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text, flags=re.I)
  text = re.sub(r"([A-Za-z0-9._%+-]{2})[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", r"\1***\2", text)
  return text[:max_len]
