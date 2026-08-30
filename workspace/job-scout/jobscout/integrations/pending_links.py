"""Atomic cache adapter for account-link setup URLs used by Job Scout."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

from ..config import PENDING_LINKS_PATH


DEFAULT_CACHE_PATH = PENDING_LINKS_PATH


def utc_now() -> datetime:
  return datetime.now(timezone.utc)


def utc_now_iso() -> str:
  return utc_now().isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
  if not value:
    return None
  text = str(value).strip()
  if text.endswith("Z"):
    text = f"{text[:-1]}+00:00"
  try:
    parsed = datetime.fromisoformat(text)
  except ValueError:
    return None
  if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=timezone.utc)
  return parsed.astimezone(timezone.utc)


def normalize_phone(value: str) -> str:
  digits = re.sub(r"\D", "", str(value or ""))
  if not digits:
    raise SystemExit("phone is required")
  phone = f"+{digits}"
  if len(phone) < 9 or len(phone) > 16:
    raise SystemExit("phone must normalize to 9-16 digits")
  return phone


def normalize_key(value: str | None, fallback: str) -> str:
  text = str(value or fallback).strip()
  return text or fallback


def is_usable(entry: dict, now: datetime | None = None) -> bool:
  if not isinstance(entry, dict):
    return False
  if not str(entry.get("setupUrl") or "").strip():
    return False
  expires_at = parse_time(entry.get("expiresAt"))
  return bool(expires_at and expires_at > (now or utc_now()))


def read_links(cache_path: Path = DEFAULT_CACHE_PATH) -> list[dict]:
  try:
    raw = cache_path.read_text(encoding="utf-8")
  except FileNotFoundError:
    return []
  try:
    parsed = json.loads(raw)
  except json.JSONDecodeError:
    return []
  if isinstance(parsed, list):
    links = parsed
  elif isinstance(parsed, dict) and isinstance(parsed.get("links"), list):
    links = parsed["links"]
  else:
    links = []
  return [entry for entry in links if isinstance(entry, dict)]


def write_links(links: list[dict], cache_path: Path = DEFAULT_CACHE_PATH) -> None:
  usable = [entry for entry in links if is_usable(entry)]
  cache_path.parent.mkdir(parents=True, exist_ok=True)
  tmp = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
  tmp.write_text(json.dumps({"version": 1, "links": usable}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  os.chmod(tmp, 0o600)
  tmp.replace(cache_path)


def criteria(
  *,
  phone: str,
  phone_hash: str,
  service: str,
  purpose: str,
  next_path: str,
) -> dict[str, str]:
  return {
    "phone": normalize_phone(phone),
    "phoneHash": str(phone_hash),
    "service": normalize_key(service, "account-link"),
    "purpose": normalize_key(purpose, "account"),
    "nextPath": normalize_key(next_path, "/"),
  }


def matches(entry: dict, values: dict[str, str]) -> bool:
  return all(entry.get(key) == value for key, value in values.items())


def load_usable_links(cache_path: Path = DEFAULT_CACHE_PATH) -> list[dict]:
  links = read_links(cache_path)
  usable = [entry for entry in links if is_usable(entry)]
  if len(usable) != len(links):
    write_links(usable, cache_path)
  return usable


def get_pending_link(values: dict[str, str], cache_path: Path = DEFAULT_CACHE_PATH) -> dict | None:
  for entry in load_usable_links(cache_path):
    if matches(entry, values):
      return {**entry, "ok": True, "cached": True}
  return None


def store_pending_link(entry: dict, cache_path: Path = DEFAULT_CACHE_PATH) -> dict:
  values = {key: entry[key] for key in ("phone", "phoneHash", "service", "purpose", "nextPath")}
  links = [item for item in load_usable_links(cache_path) if not matches(item, values)]
  record = {
    "phone": entry["phone"],
    "phoneHash": entry["phoneHash"],
    "service": entry["service"],
    "purpose": entry["purpose"],
    "nextPath": entry["nextPath"],
    "setupUrl": entry["setupUrl"],
    "expiresAt": entry["expiresAt"],
    "createdAt": entry.get("createdAt") or utc_now_iso(),
  }
  links.append(record)
  write_links(links, cache_path)
  return {**record, "ok": True, "cached": False}


def remove_pending_links(
  *,
  phone: str | None = None,
  phone_hash: str | None = None,
  service: str | None = None,
  purpose: str | None = None,
  next_path: str | None = None,
  cache_path: Path = DEFAULT_CACHE_PATH,
) -> int:
  links = load_usable_links(cache_path)
  normalized_phone = normalize_phone(phone) if phone else None
  removed = 0
  kept = []
  for entry in links:
    should_remove = True
    if normalized_phone and entry.get("phone") != normalized_phone:
      should_remove = False
    if phone_hash and entry.get("phoneHash") != phone_hash:
      should_remove = False
    if service and entry.get("service") != service:
      should_remove = False
    if purpose and entry.get("purpose") != purpose:
      should_remove = False
    if next_path and entry.get("nextPath") != next_path:
      should_remove = False
    if should_remove:
      removed += 1
    else:
      kept.append(entry)
  if removed:
    write_links(kept, cache_path)
  return removed
