#!/usr/bin/env python3
"""Manage Webetu restaurant preferences through Agent Genaie's internal API."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


LIB_DIR = "/home/joseph/.openclaw/workspace/lib"
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)
from genaie_env import agent_base_url, api_key, endpoint, env_default

DEFAULT_WORKER_SAFE = "/usr/local/bin/webetu-worker-safe"
DEFAULT_WORKER_USER = "webetu-worker"


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
        print(f"Agent Genaie Webetu preferences API failed with HTTP {err.code}: {body}", file=sys.stderr)
        raise SystemExit(1) from err
    except urllib.error.URLError as err:
        print(f"Could not reach Agent Genaie Webetu preferences API: {err}", file=sys.stderr)
        raise SystemExit(1) from err


def worker_safe_path() -> str:
    return env_default("WEBETU_WORKER_SAFE", DEFAULT_WORKER_SAFE)


def worker_user() -> str:
    return env_default("WEBETU_WORKER_USER", DEFAULT_WORKER_USER)


def worker_base_command() -> list[str]:
    if env_default("WEBETU_WORKER_NO_SUDO", "").lower() in {"1", "true", "yes", "on"}:
        return [worker_safe_path()]
    return ["sudo", "-n", "-u", worker_user(), worker_safe_path()]


def request_worker_json(args: list[str]) -> dict:
    command = [*worker_base_command(), *args]
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Could not run Webetu worker safely: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "Webetu worker failed").strip()
        print(message, file=sys.stderr)
        raise SystemExit(completed.returncode)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        print("Webetu worker returned invalid JSON.", file=sys.stderr)
        raise SystemExit(1) from exc


def normalize_restaurant_lookup(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def linked_user_id_for_phone(phone: str) -> str | None:
    query = urllib.parse.urlencode({"phone": normalize_phone(phone)})
    payload = request_json("GET", endpoint(f"/internal/account-link/status?{query}"))
    if payload.get("linked") is True and payload.get("userId"):
        return str(payload["userId"])
    return None


def public_restaurant(entry: dict | None) -> dict | None:
    if not isinstance(entry, dict):
        return None
    name = str(entry.get("name") or entry.get("nameAR") or entry.get("nameFR") or "").strip()
    if not name:
        return None
    result: dict[str, object] = {"name": name}
    meals = []
    if entry.get("breakfast") is True:
        meals.append("breakfast")
    if entry.get("lunch") is True:
        meals.append("lunch")
    if entry.get("dinner") is True:
        meals.append("dinner")
    if meals:
        result["meals"] = meals
    return result


def public_result(payload: dict) -> dict:
    result = {"ok": bool(payload.get("ok", True))}
    for key in ("linked", "maskedPhone", "status", "webetuStatus", "date"):
        if key in payload:
            result[key] = payload[key]
    if "restaurants" in payload:
        result["restaurants"] = [public_restaurant(entry) for entry in payload.get("restaurants", [])]
        result["restaurants"] = [entry for entry in result["restaurants"] if entry]
        result["count"] = len(result["restaurants"])
    if "defaultRestaurant" in payload:
        result["defaultRestaurant"] = public_restaurant(payload.get("defaultRestaurant"))
    if "restaurant" in payload:
        result["restaurant"] = public_restaurant(payload.get("restaurant"))
    return result


def require_confirmed(args: argparse.Namespace) -> None:
    if args.dry_run:
        return
    if not args.confirmed:
        raise SystemExit("Refusing to save without --confirmed. Confirm the restaurant with the user first.")


def live_restaurants_for_selector(phone: str | None = None, user: str | None = None) -> dict:
    if phone:
        phone = normalize_phone(phone)
        linked_user_id = linked_user_id_for_phone(phone)
        if linked_user_id:
            return request_worker_json(["list-restaurants", "--user", linked_user_id])
        return request_worker_json(["list-restaurants", "--phone", phone])
    if user:
        return request_worker_json(["list-restaurants", "--user", user])
    raise SystemExit("list-restaurants requires --phone after the user has saved Webetu credentials.")


def restaurant_candidates(entry: dict) -> set[str]:
    return {
        normalize_restaurant_lookup(entry.get("name")),
        normalize_restaurant_lookup(entry.get("nameAR")),
        normalize_restaurant_lookup(entry.get("nameFR")),
    } - {""}


def resolve_live_restaurant_for_phone(phone: str, restaurant_name: str) -> dict:
    payload = live_restaurants_for_selector(phone=phone)
    restaurants = payload.get("restaurants")
    if not isinstance(restaurants, list):
        raise SystemExit("Webetu worker did not return a restaurant list.")
    lookup = normalize_restaurant_lookup(restaurant_name)
    for entry in restaurants:
        if isinstance(entry, dict) and lookup in restaurant_candidates(entry):
            return entry
    public_names = [public_restaurant(entry) for entry in restaurants]
    public_names = [str(entry["name"]) for entry in public_names if entry]
    if public_names:
        print("That restaurant was not found in this user's live Webetu list. Supported names:", file=sys.stderr)
        for name in public_names:
            print(f"- {name}", file=sys.stderr)
    raise SystemExit(1)


def cmd_list_restaurants(args: argparse.Namespace) -> dict:
    if args.dry_run:
        selector = {"phoneHash": phone_hash(args.phone)} if args.phone else {"user": args.user}
        return {"dryRun": True, **selector, "workerCommand": ["list-restaurants", "--phone" if args.phone else "--user", args.phone or args.user]}
    return public_result(live_restaurants_for_selector(phone=args.phone, user=args.user))


def cmd_get(args: argparse.Namespace) -> dict:
    phone = normalize_phone(args.phone)
    query = urllib.parse.urlencode({"phone": phone})
    if args.dry_run:
        return {"dryRun": True, "phoneHash": phone_hash(phone), "endpoint": endpoint(f"/internal/webetu/preferences?{query}")}
    return public_result(request_json("GET", endpoint(f"/internal/webetu/preferences?{query}")))


def cmd_set_default(args: argparse.Namespace) -> dict:
    require_confirmed(args)
    phone = normalize_phone(args.phone)
    restaurant = resolve_live_restaurant_for_phone(phone, args.restaurant) if not args.dry_run else None
    if args.dry_run:
        return {
            "dryRun": True,
            "phoneHash": phone_hash(phone),
            "restaurant": args.restaurant,
            "endpoint": endpoint("/internal/webetu/preferences/default"),
        }
    return public_result(
        request_json(
            "POST",
            endpoint("/internal/webetu/preferences/default"),
            {"phone": phone, "restaurant": restaurant},
        )
    )


def cmd_set_override(args: argparse.Namespace) -> dict:
    require_confirmed(args)
    phone = normalize_phone(args.phone)
    restaurant = resolve_live_restaurant_for_phone(phone, args.restaurant) if not args.dry_run else None
    if args.dry_run:
        return {
            "dryRun": True,
            "phoneHash": phone_hash(phone),
            "date": args.date,
            "restaurant": args.restaurant,
            "endpoint": endpoint("/internal/webetu/preferences/override"),
        }
    return public_result(
        request_json(
            "POST",
            endpoint("/internal/webetu/preferences/override"),
            {"phone": phone, "date": args.date, "restaurant": restaurant},
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Webetu restaurant preferences.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_restaurants = subparsers.add_parser("list-restaurants", help="List supported restaurant names")
    selector = list_restaurants.add_mutually_exclusive_group(required=True)
    selector.add_argument("--phone", help="WhatsApp phone number")
    selector.add_argument("--user", help="Worker user id")
    list_restaurants.add_argument("--dry-run", action="store_true")
    list_restaurants.set_defaults(func=cmd_list_restaurants)

    get = subparsers.add_parser("get", help="Get a user's saved Webetu restaurant preference")
    get.add_argument("--phone", required=True, help="WhatsApp phone number")
    get.add_argument("--dry-run", action="store_true")
    get.set_defaults(func=cmd_get)

    default = subparsers.add_parser("set-default", help="Save a user's default Webetu restaurant")
    default.add_argument("--phone", required=True, help="WhatsApp phone number")
    default.add_argument("--restaurant", required=True, help="Supported restaurant name")
    default.add_argument("--confirmed", action="store_true", help="Required after the user confirms the restaurant")
    default.add_argument("--dry-run", action="store_true")
    default.set_defaults(func=cmd_set_default)

    override = subparsers.add_parser("set-override", help="Save a one-day Webetu restaurant override")
    override.add_argument("--phone", required=True, help="WhatsApp phone number")
    override.add_argument("--date", required=True, help="Reservation date as YYYY-MM-DD")
    override.add_argument("--restaurant", required=True, help="Supported restaurant name")
    override.add_argument("--confirmed", action="store_true", help="Required after the user confirms the restaurant")
    override.add_argument("--dry-run", action="store_true")
    override.set_defaults(func=cmd_set_override)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = args.func(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
