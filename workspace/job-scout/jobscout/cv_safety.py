"""Shared validation for canonical and personalized CV HTML."""

from __future__ import annotations

import html
import re


BROWSER_TIMESTAMP = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4},?\s+\d{1,2}:\d{2}\s+(?:AM|PM)$", re.IGNORECASE)
PAGE_COUNTER = re.compile(r"^\d+\s*/\s*\d+$")


def visible_lines_from_html(source: str) -> list[str]:
  without_styles = re.sub(r"<style\b[^>]*>.*?</style>", "\n", source, flags=re.IGNORECASE | re.DOTALL)
  visible = html.unescape(re.sub(r"<[^>]+>", "\n", without_styles))
  return [re.sub(r"\s+", " ", line).strip() for line in visible.splitlines() if line.strip()]


def validate_cv_html_source(source: str) -> str:
  text = str(source or "")
  if not text.strip():
    raise RuntimeError("CV HTML is empty")
  if not re.search(r"<html(?:\s|>)", text, flags=re.IGNORECASE) or not re.search(r"<body(?:\s|>)", text, flags=re.IGNORECASE):
    raise RuntimeError("CV HTML must contain html and body elements")
  if re.search(r"<(?:script|iframe|object|embed|base)\b", text, flags=re.IGNORECASE):
    raise RuntimeError("CV HTML contains an unsafe element")
  if re.search(r"\son[a-z]+\s*=|(?:href|src)\s*=\s*['\"]?\s*javascript:", text, flags=re.IGNORECASE):
    raise RuntimeError("CV HTML contains executable content")
  if re.search(r"(?:href|src|srcset|poster|action)\s*=\s*['\"]?\s*(?:https?:)?//", text, flags=re.IGNORECASE):
    raise RuntimeError("CV HTML must be self-contained")
  if re.search(r"(?:@import\s+(?:url\()?|url\()\s*['\"]?\s*(?:https?:)?//", text, flags=re.IGNORECASE):
    raise RuntimeError("CV HTML must be self-contained")
  lines = visible_lines_from_html(text)
  if any(line.casefold().startswith(("file://", "about:blank")) for line in lines):
    raise RuntimeError("CV HTML contains browser print chrome")
  if any(BROWSER_TIMESTAMP.fullmatch(line) or PAGE_COUNTER.fullmatch(line) for line in lines):
    raise RuntimeError("CV HTML contains browser print chrome")
  return text
