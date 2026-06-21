#!/usr/bin/env python3
"""Manage WhatsApp-to-login account links through Agent Genaie's internal API."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from pending_cache import (
  criteria as pending_criteria,
  get_pending_link,
  remove_pending_links,
  store_pending_link,
  utc_now_iso,
)


LIB_DIR = "/home/joseph/.openclaw/workspace/lib"
if LIB_DIR not in sys.path:
  sys.path.insert(0, LIB_DIR)
from genaie_env import agent_base_url, api_key, endpoint, env_default


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


def normalize_purpose(value: str) -> str:
  purpose = re.sub(r"[^a-z0-9_-]", "_", str(value or "account").strip().lower())[:40]
  return purpose or "account"


def normalize_next_path(value: str) -> str:
  text = str(value or "/").strip()
  if not text or not text.startswith("/") or text.startswith("//"):
    return "/"
  if text in {"/", "/connect-gmail", "/vault"}:
    return text
  match = re.match(r"^/(connect-gmail|vault)/?(\?.*)?$", text)
  if match:
    return f"/{match.group(1)}{match.group(2) or ''}"
  return "/"


def request_json(method: str, url: str, payload: dict | None = None) -> dict:
  key = api_key()
  if not key:
    raise SystemExit("Missing AGENT_GENAI_INTERNAL_API_KEY.")
  data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
  request = urllib.request.Request(
    url,
    data=data,
    method=method,
    headers={
      "Authorization": f"Bearer {key}",
      "Content-Type": "application/json",
    },
  )
  try:
    with urllib.request.urlopen(request, timeout=45) as response:
      body = response.read().decode("utf-8")
      return json.loads(body) if body else {}
  except urllib.error.HTTPError as err:
    body = err.read().decode("utf-8", errors="replace")
    print(f"Agent Genaie account-link API failed with HTTP {err.code}: {body}", file=sys.stderr)
    raise SystemExit(1) from err
  except urllib.error.URLError as err:
    print(f"Could not reach Agent Genaie account-link API: {err}", file=sys.stderr)
    raise SystemExit(1) from err


def cmd_create_invite(args: argparse.Namespace) -> dict:
  phone = normalize_phone(args.phone)
  purpose = normalize_purpose(args.purpose)
  next_path = normalize_next_path(args.next_path)
  cache_key = pending_criteria(
    phone=phone,
    phone_hash=phone_hash(phone),
    service="account-link",
    purpose=purpose,
    next_path=next_path,
  )
  if args.dry_run:
    return {
      "dryRun": True,
      "phoneHash": phone_hash(phone),
      "endpoint": endpoint("/internal/account-link/invite"),
      "purpose": purpose,
      "nextPath": next_path,
    }
  cached = get_pending_link(cache_key)
  if cached:
    return cached
  result = request_json(
    "POST",
    endpoint("/internal/account-link/invite"),
    {
      "phone": phone,
      "purpose": purpose,
      "nextPath": next_path,
      "ttlSeconds": args.ttl_seconds,
    },
  )
  if result.get("setupUrl") and result.get("expiresAt"):
    cached_result = store_pending_link(
      {
        **cache_key,
        "setupUrl": result["setupUrl"],
        "expiresAt": result["expiresAt"],
        "createdAt": result.get("createdAt") or utc_now_iso(),
      }
    )
    return {**result, **cached_result}
  return result


def cmd_status(args: argparse.Namespace) -> dict:
  phone = normalize_phone(args.phone)
  query = urllib.parse.urlencode({"phone": phone})
  if args.dry_run:
    return {"dryRun": True, "phoneHash": phone_hash(phone), "endpoint": endpoint(f"/internal/account-link/status?{query}")}
  result = request_json("GET", endpoint(f"/internal/account-link/status?{query}"))
  if result.get("linked"):
    remove_pending_links(phone=phone, service="account-link")
  return result


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(description="Create/check Agent Genaie account-link invites.")
  subparsers = parser.add_subparsers(dest="command", required=True)

  invite = subparsers.add_parser("create-invite", help="Create a WhatsApp-to-login link invite")
  invite.add_argument("--phone", required=True, help="WhatsApp phone number")
  invite.add_argument("--purpose", default="account", help="Service purpose label, e.g. webetu, jobs, news")
  invite.add_argument("--next-path", default="/", help="Signed-in path to open after linking")
  invite.add_argument("--ttl-seconds", type=int, default=24 * 60 * 60)
  invite.add_argument("--dry-run", action="store_true")
  invite.set_defaults(func=cmd_create_invite)

  status = subparsers.add_parser("status", help="Check whether a WhatsApp phone is linked")
  status.add_argument("--phone", required=True, help="WhatsApp phone number")
  status.add_argument("--dry-run", action="store_true")
  status.set_defaults(func=cmd_status)
  return parser


def main(argv: list[str] | None = None) -> int:
  parser = build_parser()
  args = parser.parse_args(argv)
  result = args.func(args)
  print(json.dumps(result, ensure_ascii=False, indent=2))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
