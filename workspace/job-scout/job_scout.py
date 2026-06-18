#!/usr/bin/env python3
"""Manage Job Scout setup through Agent Genaie's internal API."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


WORKSPACE = Path("/home/joseph/.openclaw/workspace")
DEFAULT_STORAGE_ROOT = WORKSPACE / "job-scout" / "users"
DEFAULT_DISPATCH_ROOT = WORKSPACE / "job-scout" / "dispatch-runs"
DISPATCH_STATUSES = {"applied", "physical_submission", "no_matches", "failed"}
DEFAULT_JSEARCH_COUNTRY = "dz"
DEFAULT_JSEARCH_LANGUAGE = "fr"
DEFAULT_JSEARCH_DATE_POSTED = "3days"
DEFAULT_OPENWEBNINJA_BASE_URL = "https://api.openwebninja.com/jsearch"
DEFAULT_RAPIDAPI_BASE_URL = "https://jsearch.p.rapidapi.com"
DEFAULT_RAPIDAPI_HOST = "jsearch.p.rapidapi.com"
GENAIE_MAIL_BIN = Path("/home/joseph/.openclaw/workspace/bin/genaie-mail")
OPENCLAW_BIN = Path(os.environ.get("OPENCLAW_BIN", "/home/joseph/.npm-global/bin/openclaw"))
PUBLIC_DISPATCH_FAILURE_MESSAGE = (
  "Job Scout could not finish today's job check because the service failed. "
  "I recorded it and will try again on the next run."
)
os.environ.setdefault(
  "OPENCLAW_PENDING_LINKS_PATH",
  "/home/joseph/.openclaw/runtime/account-link/pending-links.json",
)
ACCOUNT_LINK_DIR = WORKSPACE / "account-link"
if str(ACCOUNT_LINK_DIR) not in sys.path:
  sys.path.insert(0, str(ACCOUNT_LINK_DIR))

LIB_DIR = str(WORKSPACE / "lib")
if LIB_DIR not in sys.path:
  sys.path.insert(0, LIB_DIR)

from genaie_env import (
  agent_base_url,
  agent_base_url_source,
  api_key,
  endpoint,
  env_default,
)
from pending_cache import (
  criteria as pending_criteria,
  get_pending_link,
  remove_pending_links,
  store_pending_link,
  utc_now_iso,
)


class JSearchUnavailable(RuntimeError):
  """Transient JSearch failure (HTTP/network). Non-fatal: the run continues with the
  scraped boards so a JSearch outage can't take the rest of dispatch down with it.
  Missing-credential errors stay as SystemExit (real misconfiguration, must be loud)."""


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


def user_key(user_id: str) -> str:
  text = str(user_id or "").strip()
  if not text:
    raise SystemExit("user-id is required")
  return hashlib.sha256(f"job-scout-user:{text}".encode("utf-8")).hexdigest()[:16]


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


def pending_links_path() -> Path:
  return Path(
    os.environ.get(
      "OPENCLAW_PENDING_LINKS_PATH",
      "/home/joseph/.openclaw/runtime/account-link/pending-links.json",
    )
  ).expanduser()


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
    print(f"Agent Genaie Job Scout API failed with HTTP {err.code}: {body}", file=sys.stderr)
    raise SystemExit(1) from err
  except urllib.error.URLError as err:
    print(f"Could not reach Agent Genaie Job Scout API: {err}", file=sys.stderr)
    raise SystemExit(1) from err


def clean_items(values: list[str] | None) -> list[str]:
  output: list[str] = []
  seen: set[str] = set()
  if isinstance(values, str):
    iterable = [values]
  else:
    iterable = values or []
  for value in iterable:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
      continue
    key = text.casefold()
    if key in seen:
      continue
    seen.add(key)
    output.append(text)
  return output


def profile_values(profile: dict, key: str) -> list[str]:
  value = profile.get(key, [])
  if isinstance(value, list):
    return value
  if value is None:
    return []
  return [str(value)]


def preference_code(profile: dict, key: str, default: str) -> str:
  text = re.sub(r"[^A-Za-z]", "", str(profile.get(key) or default)).lower()
  return text if re.fullmatch(r"[a-z]{2}", text) else default


def max_applications(profile: dict, default: int = 2) -> int:
  try:
    value = int(profile.get("maxApplicationsPerRun", default))
  except (TypeError, ValueError):
    value = default
  return max(0, min(value, 5))


def load_profile_json(value: str | None) -> dict:
  if not value:
    return {}
  if value.startswith("@"):
    return json.loads(Path(value[1:]).read_text(encoding="utf-8"))
  return json.loads(value)


def linked_user_id(phone: str) -> str:
  query = urllib.parse.urlencode({"phone": normalize_phone(phone)})
  result = request_json("GET", endpoint(f"/internal/account-link/status?{query}"))
  user_id = str(result.get("userId") or "").strip()
  if not result.get("linked") or not user_id:
    raise SystemExit("phone is not linked to an Agent Genaie user")
  return user_id


def subscriber_preferences(subscriber: dict) -> dict:
  preferences = subscriber.get("preferences")
  return preferences if isinstance(preferences, dict) else {}


def subscriber_user_id(subscriber: dict) -> str:
  for key in ("userId", "uid", "id"):
    value = str(subscriber.get(key) or "").strip()
    if value:
      return value
  raise SystemExit("subscriber is missing userId")


def search_queries(preferences: dict) -> list[str]:
  roles = clean_items(profile_values(preferences, "targetRoles") or profile_values(preferences, "roles"))
  locations = clean_items(profile_values(preferences, "locations"))
  if not roles:
    return []
  if not locations:
    locations = ["Algeria"]
  queries: list[str] = []
  seen: set[str] = set()
  for role in roles:
    for location in locations:
      query = f"{role} à {location}".strip()
      key = query.casefold()
      if key in seen:
        continue
      seen.add(key)
      queries.append(query)
  return queries


def jsearch_provider() -> str:
  requested = env_default("JSEARCH_PROVIDER", "auto").strip().lower() or "auto"
  if requested not in {"auto", "openwebninja", "rapidapi"}:
    raise SystemExit("JSEARCH_PROVIDER must be auto, openwebninja, or rapidapi")
  if requested != "auto":
    return requested
  if env_default("JSEARCH_API_KEY"):
    return "openwebninja"
  if env_default("JSEARCH_RAPIDAPI_KEY") or env_default("RAPIDAPI_KEY"):
    return "rapidapi"
  raise SystemExit("Missing JSearch credentials: set JSEARCH_API_KEY or JSEARCH_RAPIDAPI_KEY.")


def jsearch_request(query: str, country: str, language: str, limit: int, cursor: str | None = None) -> dict:
  provider = jsearch_provider()
  if provider == "rapidapi":
    key = env_default("JSEARCH_RAPIDAPI_KEY") or env_default("RAPIDAPI_KEY")
    if not key:
      raise SystemExit("Missing JSEARCH_RAPIDAPI_KEY.")
    base_url = env_default("JSEARCH_RAPIDAPI_BASE_URL", DEFAULT_RAPIDAPI_BASE_URL).rstrip("/")
    host = env_default("JSEARCH_RAPIDAPI_HOST", DEFAULT_RAPIDAPI_HOST)
    params = {
      "query": query,
      "country": country,
      "date_posted": env_default("JSEARCH_DATE_POSTED", DEFAULT_JSEARCH_DATE_POSTED),
      "num_pages": "1",
      "page": cursor or "1",
    }
    headers = {
      "X-RapidAPI-Key": key,
      "X-RapidAPI-Host": host,
    }
    url = f"{base_url}/search?{urllib.parse.urlencode(params)}"
  else:
    key = env_default("JSEARCH_API_KEY")
    if not key:
      raise SystemExit("Missing JSEARCH_API_KEY.")
    base_url = env_default("JSEARCH_BASE_URL", DEFAULT_OPENWEBNINJA_BASE_URL).rstrip("/")
    params = {
      "query": query,
      "country": country,
      "language": language,
      "date_posted": env_default("JSEARCH_DATE_POSTED", DEFAULT_JSEARCH_DATE_POSTED),
      "num": str(max(1, min(limit, 25))),
    }
    if cursor:
      params["cursor"] = cursor
    headers = {"x-api-key": key}
    url = f"{base_url}/search-v2?{urllib.parse.urlencode(params)}"

  request = urllib.request.Request(url, headers={**headers, "Accept": "application/json"})
  try:
    with urllib.request.urlopen(request, timeout=45) as response:
      body = response.read().decode("utf-8")
      return json.loads(body) if body else {}
  except urllib.error.HTTPError as err:
    body = err.read().decode("utf-8", errors="replace")
    print(f"JSearch request failed with HTTP {err.code}: {body[:1000]}", file=sys.stderr)
    raise JSearchUnavailable(f"HTTP {err.code}") from err
  except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as err:
    # Connection refused/reset, DNS, and read timeouts (socket TimeoutError) are all
    # transient; treat them like the HTTP errors above so dispatch falls back to boards.
    print(f"Could not reach JSearch API: {err}", file=sys.stderr)
    raise JSearchUnavailable("network error") from err


def jsearch_items(response: dict) -> list[dict]:
  for key in ("data", "jobs", "results"):
    value = response.get(key)
    if isinstance(value, list):
      return [item for item in value if isinstance(item, dict)]
  return []


EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)


def extract_emails(*values: object) -> list[str]:
  emails: list[str] = []
  seen: set[str] = set()
  for value in values:
    for email in EMAIL_RE.findall(str(value or "")):
      key = email.casefold()
      if key in seen:
        continue
      seen.add(key)
      emails.append(email)
  return emails


def apply_links(item: dict) -> list[str]:
  links: list[str] = []
  for key in ("job_apply_link", "job_google_link", "employer_website"):
    value = str(item.get(key) or "").strip()
    if value:
      links.append(value)
  options = item.get("apply_options")
  if isinstance(options, list):
    for option in options:
      if not isinstance(option, dict):
        continue
      value = str(option.get("apply_link") or option.get("link") or "").strip()
      if value:
        links.append(value)
  output: list[str] = []
  seen: set[str] = set()
  for link in links:
    if link in seen:
      continue
    seen.add(link)
    output.append(link)
  return output


def normalized_candidate(item: dict, query: str) -> dict:
  company = re.sub(r"\s+", " ", str(item.get("employer_name") or item.get("company") or "Unknown employer")).strip()
  role = re.sub(r"\s+", " ", str(item.get("job_title") or item.get("title") or "Unknown role")).strip()
  links = apply_links(item)
  description = re.sub(r"\s+", " ", str(item.get("job_description") or item.get("description") or "")).strip()
  emails = extract_emails(description, item.get("job_apply_link"), item.get("apply_options"))
  location = ", ".join(clean_items([
    item.get("job_city"),
    item.get("job_state"),
    item.get("job_country"),
  ]))
  source_url = links[0] if links else None
  raw_id = str(item.get("job_id") or item.get("id") or "").strip()
  fallback_id = hashlib.sha256(f"{company}\n{role}\n{source_url or ''}".encode("utf-8")).hexdigest()[:24]
  return {
    "candidateId": raw_id or fallback_id,
    "source": "jsearch",
    "sourceUrl": source_url,
    "applyLinks": links,
    "applicationEmails": emails,
    "applicationEmail": emails[0] if emails else None,
    "company": company,
    "role": role,
    "location": location,
    "query": query,
    "closing": item.get("job_offer_expiration_datetime_utc") or item.get("job_offer_expiration_datetime"),
    "description": description[:2000],
  }


def candidate_key(candidate: dict) -> str:
  return hashlib.sha256(
    "\n".join([
      str(candidate.get("company") or "").casefold(),
      str(candidate.get("role") or "").casefold(),
      str(candidate.get("sourceUrl") or "").casefold(),
      str(candidate.get("candidateId") or "").casefold(),
    ]).encode("utf-8")
  ).hexdigest()


def application_key(value: dict) -> tuple[str, str]:
  return (
    str(value.get("company") or "").strip().casefold(),
    str(value.get("role") or "").strip().casefold(),
  )


def filter_candidates(candidates: list[dict], preferences: dict, applications: list[dict], limit: int) -> list[dict]:
  exclusions = [item.casefold() for item in clean_items(profile_values(preferences, "exclusions"))]
  existing = {application_key(item) for item in applications}
  seen: set[str] = set()
  output: list[dict] = []
  for candidate in candidates:
    text = " ".join([
      str(candidate.get("company") or ""),
      str(candidate.get("role") or ""),
      str(candidate.get("description") or ""),
    ]).casefold()
    if any(exclusion in text for exclusion in exclusions):
      continue
    appkey = application_key(candidate)
    # Skip jobs already applied to (prior runs) and collapse same company/role
    # across sources within this run, so a JSearch + board duplicate is applied once.
    if appkey in existing:
      continue
    key = candidate_key(candidate)
    if key in seen:
      continue
    seen.add(key)
    existing.add(appkey)
    output.append(candidate)
    if len(output) >= limit:
      break
  return output


def fetch_jsearch_candidates(preferences: dict, limit: int) -> list[dict]:
  country = preference_code(preferences, "country", DEFAULT_JSEARCH_COUNTRY)
  language = preference_code(preferences, "language", DEFAULT_JSEARCH_LANGUAGE)
  candidates: list[dict] = []
  for query in search_queries(preferences):
    try:
      response = jsearch_request(query, country, language, limit)
    except JSearchUnavailable as err:
      # Transient JSearch outage: keep any candidates already gathered and let the
      # caller fall back to the scraped boards instead of aborting the whole run.
      print(f"JSearch unavailable, continuing without it: {public_text(err)}", file=sys.stderr)
      break
    for item in jsearch_items(response):
      candidates.append(normalized_candidate(item, query))
    if len(candidates) >= limit:
      break
  return candidates


# Zimbabwe job boards (VacancyMail, iHarare, VacancyBox) are scraped to extend JSearch
# for ZW-targeting subscribers only. JSearch still runs for everyone.
ZIMBABWE_HINTS = {
  "zimbabwe", "harare", "bulawayo", "mutare", "gweru", "kwekwe", "masvingo",
  "chitungwiza", "kadoma", "chinhoyi", "marondera", "victoria falls", "zvishavane",
  "bindura", "beitbridge", "hwange", "redcliff", "norton", "ruwa", "chegutu",
  "kariba", "rusape", "chiredzi", "gwanda", "midlands",
}


def zw_targeted(preferences: dict) -> bool:
  country_raw = str(preferences.get("country") or "").strip().casefold()
  if country_raw == "zw" or "zimbabwe" in country_raw:
    return True
  if preference_code(preferences, "country", DEFAULT_JSEARCH_COUNTRY) == "zw":
    return True
  for location in clean_items(profile_values(preferences, "locations")):
    text = location.casefold()
    if any(hint in text for hint in ZIMBABWE_HINTS):
      return True
  return False


def fetch_scraped_candidates(preferences: dict, limit: int) -> list[dict]:
  """Board-scraped candidates for ZW-targeting users. Never raises; merged alongside
  JSearch results. Returns [] when not ZW-targeted or the scrapers are unavailable."""
  if limit <= 0 or not zw_targeted(preferences):
    return []
  try:
    import scrapers  # lazily imported so a scraper import error can't break JSearch
    return scrapers.fetch_scraped_candidates(preferences, limit)
  except Exception as err:
    print(f"Scraper sources failed: {public_text(err)}", file=sys.stderr)
    return []


def dispatch_path(run_id: str, root: Path = DEFAULT_DISPATCH_ROOT) -> Path:
  return root / f"{safe_run_id(run_id)}.json"


def count_arg(value: int, name: str) -> int:
  if value < 0:
    raise SystemExit(f"{name} must be >= 0")
  return value


def load_dispatch(run_id: str, root: Path = DEFAULT_DISPATCH_ROOT) -> dict:
  path = dispatch_path(run_id, root)
  if not path.exists():
    raise SystemExit(f"Dispatch run not found: {safe_run_id(run_id)}")
  try:
    return json.loads(path.read_text(encoding="utf-8"))
  except json.JSONDecodeError as err:
    raise SystemExit(f"Dispatch run marker is invalid JSON: {path}") from err


def save_dispatch(data: dict, root: Path = DEFAULT_DISPATCH_ROOT) -> dict:
  run_id = safe_run_id(data.get("runId", ""))
  root.mkdir(parents=True, exist_ok=True)
  path = dispatch_path(run_id, root)
  tmp = path.with_suffix(".json.tmp")
  tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  tmp.replace(path)
  return {"ok": True, "runId": run_id, "path": str(path)}


def safe_cv_suffix(path: Path) -> str:
  suffix = path.suffix.lower()
  allowed = {".pdf", ".doc", ".docx", ".txt"}
  if suffix not in allowed:
    guessed = mimetypes.guess_type(path.name)[0] or ""
    if guessed == "application/pdf":
      return ".pdf"
    raise SystemExit("CV must be a PDF, DOC, DOCX, or TXT file for v1.")
  return suffix


def store_cv(phone: str, source: str, storage_root: Path) -> str:
  src = Path(source).expanduser().resolve()
  if not src.exists() or not src.is_file():
    raise SystemExit(f"CV file not found: {src}")
  suffix = safe_cv_suffix(src)
  user_dir = storage_root / phone_hash(phone)
  user_dir.mkdir(parents=True, exist_ok=True)
  dest = user_dir / f"cv{suffix}"
  if src != dest:
    shutil.copy2(src, dest)
  return str(dest)


def stage_cv(phone: str, source: str, storage_root: Path) -> dict:
  normalized_phone = normalize_phone(phone)
  src = Path(source).expanduser().resolve()
  cv_file_ref = store_cv(normalized_phone, str(src), storage_root)
  return {
    "ok": True,
    "phoneHash": phone_hash(normalized_phone),
    "cvFileRef": cv_file_ref,
    "originalPath": str(src),
    "storedAt": utc_now(),
  }


def cmd_create_invite(args: argparse.Namespace) -> dict:
  phone = normalize_phone(args.phone)
  cache_key = pending_criteria(
    phone=phone,
    phone_hash=phone_hash(phone),
    service="job-scout",
    purpose="jobs",
    next_path="/connect-gmail",
  )
  if args.dry_run:
    return {
      "dryRun": True,
      "phoneHash": phone_hash(phone),
      "endpoint": endpoint("/internal/job-scout/invite"),
      "service": "job-scout",
      "purpose": "jobs",
      "nextPath": "/connect-gmail",
    }
  cached = get_pending_link(cache_key)
  if cached:
    return cached
  result = request_json("POST", endpoint("/internal/job-scout/invite"), {"phone": phone, "ttlSeconds": args.ttl_seconds})
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


def cmd_check_runtime(args: argparse.Namespace) -> dict:
  cache_path = pending_links_path()
  cache_parent = cache_path.parent
  probe = cache_parent / f".{cache_path.name}.{os.getpid()}.check"
  tmp = cache_parent / f".{cache_path.name}.{os.getpid()}.tmp"
  errors: list[str] = []

  if not cache_parent.exists():
    errors.append(f"pending-link cache parent does not exist: {cache_parent}")
  elif not cache_parent.is_dir():
    errors.append(f"pending-link cache parent is not a directory: {cache_parent}")
  else:
    try:
      probe.write_text("job-scout runtime check\n", encoding="utf-8")
      os.chmod(probe, 0o600)
      tmp.write_text(json.dumps({"ok": True, "checkedAt": utc_now()}) + "\n", encoding="utf-8")
      os.chmod(tmp, 0o600)
      tmp.replace(probe)
    except OSError as err:
      errors.append(f"cannot create and replace a temp file in pending-link cache parent: {err}")
    finally:
      for path in (tmp, probe):
        try:
          path.unlink()
        except FileNotFoundError:
          pass
        except OSError:
          pass

  has_api_key = bool(api_key())
  if not has_api_key:
    errors.append("Missing AGENT_GENAI_INTERNAL_API_KEY.")

  result = {
    "ok": not errors,
    "effectiveUser": effective_user(),
    "effectiveUid": os.geteuid(),
    "pendingLinksPath": str(cache_path),
    "pendingLinksParent": str(cache_parent),
    "canWritePendingLinksParent": not any("pending-link cache" in error or "temp file" in error for error in errors),
    "hasInternalApiKey": has_api_key,
    "agentBaseUrl": agent_base_url(),
    "agentBaseUrlSource": agent_base_url_source(),
    "errors": errors,
  }
  if errors:
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(1)
  return result


def cmd_stage_cv(args: argparse.Namespace) -> dict:
  return stage_cv(args.phone, args.cv, args.storage_root)


def cmd_save_profile(args: argparse.Namespace) -> dict:
  phone = normalize_phone(args.phone)
  user_id = str(args.user_id or "").strip() or linked_user_id(phone)
  base = load_profile_json(args.profile_json)
  auto_apply = False if args.no_auto_apply else base.get("autoApply", True) is not False
  max_applications = (
    args.max_applications_per_run
    if args.max_applications_per_run is not None
    else base.get("maxApplicationsPerRun", 2)
  )
  preferences = {
    **base,
    "targetRoles": clean_items([*profile_values(base, "targetRoles"), *(args.target_role or [])]),
    "locations": clean_items([*profile_values(base, "locations"), *(args.location or [])]),
    "country": args.country or base.get("country") or DEFAULT_JSEARCH_COUNTRY,
    "language": args.language or base.get("language") or DEFAULT_JSEARCH_LANGUAGE,
    "qualifications": clean_items([*profile_values(base, "qualifications"), *(args.qualification or [])]),
    "experience": clean_items([*profile_values(base, "experience"), *(args.experience or [])]),
    "skills": clean_items([*profile_values(base, "skills"), *(args.skill or [])]),
    "exclusions": clean_items([*profile_values(base, "exclusions"), *(args.exclusion or [])]),
    "promptNotes": clean_items([*profile_values(base, "promptNotes"), *(args.prompt_note or [])]),
    "autoApply": auto_apply,
    "maxApplicationsPerRun": max_applications,
  }
  cv_file_ref = store_cv(phone, args.cv, args.storage_root)
  payload = {"phone": phone, "userId": user_id, "preferences": preferences, "cvFileRef": cv_file_ref}
  if args.dry_run:
    return {
      "dryRun": True,
      "phoneHash": phone_hash(phone),
      "userId": user_id,
      "cvFileRef": cv_file_ref,
      "preferences": preferences,
      "endpoint": endpoint("/internal/job-scout/profile"),
    }
  result = request_json("POST", endpoint("/internal/job-scout/profile"), payload)
  remove_pending_links(phone=phone, service="job-scout")
  return result


def cmd_list_ready(args: argparse.Namespace) -> dict:
  query = urllib.parse.urlencode({"limit": args.limit})
  return request_json("GET", endpoint(f"/internal/job-scout/subscribers?{query}"))


def cmd_list_applications(args: argparse.Namespace) -> dict:
  query = urllib.parse.urlencode({"userId": args.user_id})
  return request_json("GET", endpoint(f"/internal/job-scout/applications?{query}"))


def cmd_record_application(args: argparse.Namespace) -> dict:
  payload = {
    "userId": args.user_id,
    "company": args.company,
    "role": args.role,
    "status": args.status,
    "applicationEmail": args.application_email,
    "source": args.source,
    "sourceUrl": args.source_url,
    "notes": args.notes,
    "messageId": args.message_id,
    "closing": args.closing,
    "matchReason": args.match_reason,
    "replace": args.replace,
  }
  return request_json("POST", endpoint("/internal/job-scout/applications"), payload)


def cmd_fetch_candidates(args: argparse.Namespace) -> dict:
  base = load_profile_json(args.profile_json)
  preferences = {
    **base,
    "targetRoles": clean_items([*profile_values(base, "targetRoles"), *(args.target_role or [])]),
    "locations": clean_items([*profile_values(base, "locations"), *(args.location or [])]),
    "country": args.country or base.get("country") or DEFAULT_JSEARCH_COUNTRY,
    "language": args.language or base.get("language") or DEFAULT_JSEARCH_LANGUAGE,
    "exclusions": clean_items([*profile_values(base, "exclusions"), *(args.exclusion or [])]),
    "maxApplicationsPerRun": args.limit,
  }
  raw_candidates = fetch_jsearch_candidates(preferences, args.limit) + fetch_scraped_candidates(preferences, args.limit)
  candidates = filter_candidates(raw_candidates, preferences, [], args.limit)
  queries = search_queries(preferences)
  return {
    "ok": True,
    "provider": jsearch_provider() if queries else None,
    "sources": sorted({str(candidate.get("source") or "jsearch") for candidate in candidates}),
    "queries": queries,
    "country": preference_code(preferences, "country", DEFAULT_JSEARCH_COUNTRY),
    "language": preference_code(preferences, "language", DEFAULT_JSEARCH_LANGUAGE),
    "candidateCount": len(candidates),
    "candidates": candidates,
  }


def write_candidate_artifact(root: Path, run_id: str, user_id: str, data: dict) -> str:
  artifact_dir = root / safe_run_id(run_id)
  artifact_dir.mkdir(parents=True, exist_ok=True)
  path = artifact_dir / f"{user_key(user_id)}.candidates.json"
  tmp = path.with_suffix(".json.tmp")
  tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  tmp.replace(path)
  return str(path)


def applications_from_response(result: dict) -> list[dict]:
  applications = result.get("applications")
  if isinstance(applications, list):
    return [item for item in applications if isinstance(item, dict)]
  if isinstance(result, list):
    return [item for item in result if isinstance(item, dict)]
  return []


def public_text(value: object, max_len: int = 180) -> str:
  text = re.sub(r"[\r\n\t]+", " ", str(value or ""))
  text = re.sub(r"\s+", " ", text).strip()
  text = re.sub(r"/home/joseph/\S+", "[local path]", text)
  text = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text, flags=re.I)
  text = re.sub(r"([A-Za-z0-9._%+-]{2})[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", r"\1***\2", text)
  return text[:max_len]


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


def subscriber_cv_file(subscriber: dict) -> str:
  cv = str(subscriber.get("cvFileRef") or "").strip()
  if not cv:
    raise RuntimeError("subscriber is missing cvFileRef")
  return cv


def subscriber_phone_hash(subscriber: dict) -> str:
  value = str(subscriber.get("whatsappPhoneHash") or "").strip()
  if value:
    return re.sub(r"[^A-Za-z0-9_.:-]", "-", value)[:80]
  return phone_hash(subscriber_phone(subscriber))


def slug_part(value: object) -> str:
  text = re.sub(r"[^A-Za-z0-9]+", "-", str(value or "").lower())
  text = text.strip("-")
  return text[:80] or "job"


def application_artifact_dir(subscriber: dict, candidate: dict) -> Path:
  return (
    DEFAULT_STORAGE_ROOT.parent
    / "users"
    / subscriber_phone_hash(subscriber)
    / "applications"
    / slug_part(f"{candidate.get('company', '')}-{candidate.get('role', '')}")
  )


def match_reason(preferences: dict, candidate: dict) -> str:
  roles = clean_items(profile_values(preferences, "targetRoles") or profile_values(preferences, "roles"))
  role_hint = roles[0] if roles else str(candidate.get("role") or "the target role")
  return public_text(f"Matches requested work around {role_hint} and the configured location/preferences.", 220)


def cover_letter_text(subscriber: dict, preferences: dict, candidate: dict) -> str:
  name = display_name(subscriber)
  role = public_text(candidate.get("role"), 120) or "the advertised role"
  company = public_text(candidate.get("company"), 120) or "your organization"
  qualifications = clean_items(profile_values(preferences, "qualifications"))
  skills = clean_items(profile_values(preferences, "skills"))
  experience = clean_items(profile_values(preferences, "experience"))
  strengths = ", ".join([*qualifications[:2], *skills[:3], *experience[:2]]) or "relevant experience and a strong work ethic"
  return "\n".join([
    f"Dear Hiring Team at {company},",
    "",
    f"I am applying for the {role} position at {company}.",
    f"My background includes {strengths}, and I am ready to contribute reliably from the first day.",
    "I work carefully, communicate clearly, and take responsibility for completing assigned tasks.",
    "I have attached my CV for your review and would appreciate the opportunity to discuss how I can help your team.",
    "",
    f"Kind regards,",
    name,
  ]) + "\n"


def write_cover_letter(subscriber: dict, preferences: dict, candidate: dict) -> Path:
  directory = application_artifact_dir(subscriber, candidate)
  directory.mkdir(parents=True, exist_ok=True)
  path = directory / "cover-letter.txt"
  path.write_text(cover_letter_text(subscriber, preferences, candidate), encoding="utf-8")
  return path


def parse_json_output(output: str) -> dict:
  try:
    data = json.loads(output or "{}")
  except json.JSONDecodeError:
    return {}
  return data if isinstance(data, dict) else {}


def run_json_command(command: list[str], timeout: int = 90) -> dict:
  try:
    completed = subprocess.run(
      command,
      check=False,
      capture_output=True,
      text=True,
      timeout=timeout,
    )
  except (OSError, subprocess.TimeoutExpired) as err:
    raise RuntimeError(public_text(err, 240)) from err
  if completed.returncode != 0:
    detail = completed.stderr or completed.stdout or f"exit {completed.returncode}"
    raise RuntimeError(public_text(detail, 240))
  return parse_json_output(completed.stdout)


def send_application_email(subscriber: dict, candidate: dict, cover_letter: Path) -> dict:
  email = str(candidate.get("applicationEmail") or "").strip()
  if not email:
    raise RuntimeError("candidate is missing applicationEmail")
  subject = f"Application for {public_text(candidate.get('role'), 120)} - {display_name(subscriber)}"
  return run_json_command([
    str(GENAIE_MAIL_BIN),
    "send",
    "--from",
    subscriber_sender_email(subscriber),
    "--sender-uid",
    subscriber_user_id(subscriber),
    "--to",
    email,
    "--subject",
    subject,
    "--body-file",
    str(cover_letter),
    "--attach",
    subscriber_cv_file(subscriber),
    "--force",
  ])


def send_whatsapp_summary(phone: str, message: str) -> dict:
  return run_json_command([
    str(OPENCLAW_BIN),
    "message",
    "send",
    "--channel",
    "whatsapp",
    "--target",
    phone,
    "--message",
    message,
    "--json",
  ])


def result_message_id(result: dict) -> str | None:
  for key in ("messageId", "id", "gmailMessageId"):
    value = str(result.get(key) or "").strip()
    if value:
      return value
  nested = result.get("message")
  if isinstance(nested, dict):
    for key in ("id", "messageId"):
      value = str(nested.get(key) or "").strip()
      if value:
        return value
  return None


def record_candidate_application(
  user_id: str,
  candidate: dict,
  status: str,
  notes: str | None = None,
  message_id: str | None = None,
  match: str | None = None,
) -> dict:
  return cmd_record_application(argparse.Namespace(
    user_id=user_id,
    company=str(candidate.get("company") or "Unknown employer"),
    role=str(candidate.get("role") or "Unknown role"),
    status=status,
    application_email=candidate.get("applicationEmail"),
    source=str(candidate.get("source") or "jsearch"),
    source_url=candidate.get("sourceUrl"),
    notes=notes,
    message_id=message_id,
    closing=candidate.get("closing"),
    match_reason=match,
    replace=False,
  ))


def physical_instruction(candidate: dict) -> str:
  links = candidate.get("applyLinks")
  link = None
  if isinstance(links, list) and links:
    link = str(links[0] or "").strip()
  link = link or str(candidate.get("sourceUrl") or "").strip()
  if link:
    return f"Apply manually here: {link}"
  return "Manual follow-up is needed; no application email was listed."


def build_user_summary(date_label: str, applied: list[dict], physical: list[dict], failed_note: str | None = None) -> str:
  lines = [f"Job Scout - {date_label}", ""]
  if applied:
    for index, item in enumerate(applied, 1):
      candidate = item["candidate"]
      lines.extend([
        f"{index}. {public_text(candidate.get('role'), 100)} - APPLIED",
        f"Company: {public_text(candidate.get('company'), 100)}",
      ])
      location = public_text(candidate.get("location"), 100)
      if location:
        lines.append(f"Location: {location}")
      source = public_text(candidate.get("sourceUrl"), 180)
      if source:
        lines.append(f"Source: {source}")
      reason = public_text(item.get("reason"), 180)
      if reason:
        lines.append(f"Why: {reason}")
      lines.append("")
  if physical:
    for candidate in physical:
      lines.extend([
        f"- {public_text(candidate.get('role'), 100)} - manual submission needed",
        f"Company: {public_text(candidate.get('company'), 100)}",
        physical_instruction(candidate),
        "",
      ])
  if failed_note:
    lines.append(PUBLIC_DISPATCH_FAILURE_MESSAGE)
  if not applied and not physical and not failed_note:
    lines.append("No strong matches today. I will keep checking.")
  return "\n".join(line for line in lines).strip()


def cmd_dispatch_jsearch(args: argparse.Namespace) -> dict:
  ready_result = cmd_list_ready(argparse.Namespace(limit=args.limit_ready))
  subscribers = ready_result.get("subscribers") if isinstance(ready_result, dict) else []
  if not isinstance(subscribers, list):
    subscribers = []
  run_id = args.run_id or f"job-scout-jsearch-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
  run_id = safe_run_id(run_id)
  ready_count = len(subscribers)
  if not args.dry_run:
    cmd_start_dispatch(argparse.Namespace(run_id=run_id, ready_count=ready_count, dispatch_root=args.dispatch_root))

  processed_count = 0
  failed_count = 0
  no_matches_count = 0
  physical_count = 0
  applied_count = 0
  users: list[dict] = []
  date_label = datetime.now(timezone.utc).date().isoformat()

  for subscriber in subscribers:
    processed_count += 1
    user_id = subscriber_user_id(subscriber)
    preferences = subscriber_preferences(subscriber)
    per_user_limit = min(args.max_candidates_per_user, max_applications(preferences))
    queries = search_queries(preferences)
    auto_apply = preferences.get("autoApply", True) is not False
    user_applied: list[dict] = []
    user_physical: list[dict] = []
    failed_note = None
    if per_user_limit <= 0 or not queries:
      status = "no_matches"
      candidates: list[dict] = []
    else:
      try:
        applications_result = cmd_list_applications(argparse.Namespace(user_id=user_id))
        applications = applications_from_response(applications_result)
        raw_candidates = fetch_jsearch_candidates(preferences, per_user_limit) + fetch_scraped_candidates(preferences, per_user_limit)
        candidates = filter_candidates(raw_candidates, preferences, applications, per_user_limit)
      except SystemExit:
        raise
      except Exception as err:
        status = "failed"
        candidates = []
        failed_count += 1
        failed_note = public_text(err)
        users.append({
          "userKey": user_key(user_id),
          "status": status,
          "error": failed_note,
        })
        if not args.dry_run:
          try:
            send_whatsapp_summary(subscriber_phone(subscriber), build_user_summary(date_label, [], [], failed_note))
          except Exception:
            pass
          cmd_record_dispatch_outcome(argparse.Namespace(
            run_id=run_id,
            user_id=user_id,
            status=status,
            applied_count=0,
            physical_count=0,
            dispatch_root=args.dispatch_root,
          ))
        continue

    if candidates:
      for candidate in candidates:
        reason = match_reason(preferences, candidate)
        email = str(candidate.get("applicationEmail") or "").strip()
        links = candidate.get("applyLinks") if isinstance(candidate.get("applyLinks"), list) else []
        can_send = bool(email and auto_apply)
        if can_send:
          if args.dry_run:
            user_applied.append({"candidate": candidate, "reason": reason, "dryRun": True})
            continue
          try:
            cover_letter = write_cover_letter(subscriber, preferences, candidate)
            send_result = send_application_email(subscriber, candidate, cover_letter)
            record_candidate_application(
              user_id,
              candidate,
              "applied",
              message_id=result_message_id(send_result),
              match=reason,
            )
            user_applied.append({"candidate": candidate, "reason": reason})
          except Exception as err:
            failed_note = public_text(err)
            try:
              record_candidate_application(
                user_id,
                candidate,
                "failed",
                notes=failed_note,
                match=reason,
              )
            except Exception:
              pass
            break
        elif links or candidate.get("sourceUrl"):
          if not args.dry_run:
            record_candidate_application(
              user_id,
              candidate,
              "physical_submission",
              notes=physical_instruction(candidate),
              match=reason,
            )
          user_physical.append(candidate)

    if failed_note:
      status = "failed"
      failed_count += 1
    elif user_applied:
      status = "applied"
      applied_count += len(user_applied)
    elif user_physical:
      status = "physical_submission"
      physical_count += len(user_physical)
    else:
      status = "no_matches"
      no_matches_count += 1

    if not args.dry_run:
      try:
        send_whatsapp_summary(subscriber_phone(subscriber), build_user_summary(date_label, user_applied, user_physical, failed_note))
      except Exception as err:
        if status != "failed":
          status = "failed"
          failed_count += 1
          if user_applied:
            applied_count -= len(user_applied)
          elif user_physical:
            physical_count -= len(user_physical)
          else:
            no_matches_count -= 1
        failed_note = public_text(err)
        users.append({
          "userKey": user_key(user_id),
          "status": status,
          "candidateCount": len(candidates),
          "appliedCount": len(user_applied),
          "physicalCount": len(user_physical),
          "error": failed_note,
        })
        if not args.dry_run:
          cmd_record_dispatch_outcome(argparse.Namespace(
            run_id=run_id,
            user_id=user_id,
            status=status,
            applied_count=0,
            physical_count=0,
            dispatch_root=args.dispatch_root,
          ))
        continue

    artifact = {
      "schemaVersion": 1,
      "runId": run_id,
      "userKey": user_key(user_id),
      "createdAt": utc_now(),
      "provider": jsearch_provider() if queries else None,
      "sources": sorted({str(candidate.get("source") or "jsearch") for candidate in candidates}),
      "queries": queries,
      "country": preference_code(preferences, "country", DEFAULT_JSEARCH_COUNTRY),
      "language": preference_code(preferences, "language", DEFAULT_JSEARCH_LANGUAGE),
      "candidateCount": len(candidates),
      "appliedCount": len(user_applied),
      "physicalCount": len(user_physical),
      "status": status,
      "candidates": candidates,
    }
    artifact_path = None if args.dry_run else write_candidate_artifact(args.dispatch_root, run_id, user_id, artifact)
    users.append({
      "userKey": user_key(user_id),
      "status": status,
      "candidateCount": len(candidates),
      "appliedCount": len(user_applied),
      "physicalCount": len(user_physical),
      "artifactPath": artifact_path,
      "error": failed_note,
    })
    if not args.dry_run:
      cmd_record_dispatch_outcome(argparse.Namespace(
        run_id=run_id,
        user_id=user_id,
        status=status,
        applied_count=len(user_applied) if status != "failed" else 0,
        physical_count=len(user_physical) if status != "failed" else 0,
        dispatch_root=args.dispatch_root,
      ))

  if not args.dry_run:
    cmd_finish_dispatch(argparse.Namespace(
      run_id=run_id,
      ready_count=ready_count,
      processed_count=processed_count,
      applied_count=applied_count,
      physical_count=physical_count,
      failed_count=failed_count,
      no_matches_count=no_matches_count,
      dispatch_root=args.dispatch_root,
    ))
    cmd_verify_dispatch(argparse.Namespace(run_id=run_id, dispatch_root=args.dispatch_root))

  return {
    "ok": True,
    "dryRun": args.dry_run,
    "runId": run_id,
    "readyCount": ready_count,
    "processedCount": processed_count,
    "candidateCount": applied_count + physical_count,
    "appliedCount": applied_count,
    "physicalCount": physical_count,
    "failedCount": failed_count,
    "noMatchesCount": no_matches_count,
    "users": users,
  }


def cmd_start_dispatch(args: argparse.Namespace) -> dict:
  ready_count = count_arg(args.ready_count, "ready-count")
  data = {
    "schemaVersion": 1,
    "runId": safe_run_id(args.run_id),
    "startedAt": utc_now(),
    "finishedAt": None,
    "readyCount": ready_count,
    "outcomes": {},
    "finalCounts": None,
  }
  result = save_dispatch(data, args.dispatch_root)
  result["readyCount"] = ready_count
  return result


def cmd_record_dispatch_outcome(args: argparse.Namespace) -> dict:
  data = load_dispatch(args.run_id, args.dispatch_root)
  status = args.status
  if status not in DISPATCH_STATUSES:
    raise SystemExit(f"status must be one of: {', '.join(sorted(DISPATCH_STATUSES))}")
  outcomes = data.setdefault("outcomes", {})
  key = user_key(args.user_id)
  outcomes[key] = {
    "status": status,
    "appliedCount": count_arg(args.applied_count, "applied-count"),
    "physicalCount": count_arg(args.physical_count, "physical-count"),
    "recordedAt": utc_now(),
  }
  data["updatedAt"] = utc_now()
  result = save_dispatch(data, args.dispatch_root)
  result["outcomeCount"] = len(outcomes)
  return result


def cmd_finish_dispatch(args: argparse.Namespace) -> dict:
  data = load_dispatch(args.run_id, args.dispatch_root)
  counts = {
    "readyCount": count_arg(args.ready_count, "ready-count"),
    "processedCount": count_arg(args.processed_count, "processed-count"),
    "appliedCount": count_arg(args.applied_count, "applied-count"),
    "physicalCount": count_arg(args.physical_count, "physical-count"),
    "failedCount": count_arg(args.failed_count, "failed-count"),
    "noMatchesCount": count_arg(args.no_matches_count, "no-matches-count"),
  }
  data["readyCount"] = counts["readyCount"]
  data["finalCounts"] = counts
  data["finishedAt"] = utc_now()
  data["updatedAt"] = data["finishedAt"]
  result = save_dispatch(data, args.dispatch_root)
  result["finalCounts"] = counts
  return result


def cmd_verify_dispatch(args: argparse.Namespace) -> dict:
  data = load_dispatch(args.run_id, args.dispatch_root)
  errors: list[str] = []
  ready_count = int(data.get("readyCount", -1))
  outcomes = data.get("outcomes") if isinstance(data.get("outcomes"), dict) else {}
  final_counts = data.get("finalCounts") if isinstance(data.get("finalCounts"), dict) else None

  if ready_count < 0:
    errors.append("readyCount is missing or invalid")
  if not data.get("finishedAt") or not final_counts:
    errors.append("dispatch run is not finished")
  if final_counts:
    processed_count = int(final_counts.get("processedCount", -1))
    expected_ready = int(final_counts.get("readyCount", -1))
    if expected_ready != ready_count:
      errors.append("final readyCount does not match started readyCount")
    if processed_count < 0:
      errors.append("processedCount is missing or invalid")
    if ready_count == 0 and processed_count != 0:
      errors.append("processedCount must be 0 when there are no ready subscribers")
    if ready_count > 0 and processed_count != ready_count:
      errors.append("processedCount must equal readyCount")
    if len(outcomes) != processed_count:
      errors.append("recorded outcome count does not match processedCount")
    outcome_status_counts = {status: 0 for status in DISPATCH_STATUSES}
    applied_total = 0
    physical_total = 0
    for outcome in outcomes.values():
      status = outcome.get("status")
      if status not in DISPATCH_STATUSES:
        errors.append("an outcome has an invalid status")
        continue
      outcome_status_counts[status] += 1
      applied_total += int(outcome.get("appliedCount", 0))
      physical_total += int(outcome.get("physicalCount", 0))
    if int(final_counts.get("appliedCount", -1)) != applied_total:
      errors.append("appliedCount does not match recorded outcomes")
    if int(final_counts.get("physicalCount", -1)) != physical_total:
      errors.append("physicalCount does not match recorded outcomes")
    if int(final_counts.get("failedCount", -1)) != outcome_status_counts["failed"]:
      errors.append("failedCount does not match recorded outcomes")
    if int(final_counts.get("noMatchesCount", -1)) != outcome_status_counts["no_matches"]:
      errors.append("noMatchesCount does not match recorded outcomes")

  result = {
    "ok": not errors,
    "runId": safe_run_id(args.run_id),
    "readyCount": ready_count,
    "outcomeCount": len(outcomes),
    "errors": errors,
  }
  if errors:
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(1)
  return result


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(prog="job-scout")
  subparsers = parser.add_subparsers(dest="command", required=True)

  invite = subparsers.add_parser("create-invite", help="create a WhatsApp-to-app setup link")
  invite.add_argument("--phone", required=True)
  invite.add_argument("--ttl-seconds", type=int, default=24 * 60 * 60)
  invite.add_argument("--dry-run", action="store_true")
  invite.set_defaults(func=cmd_create_invite)

  check_runtime = subparsers.add_parser("check-runtime", help="check Job Scout runtime prerequisites")
  check_runtime.set_defaults(func=cmd_check_runtime)

  stage = subparsers.add_parser("stage-cv", help="copy an uploaded CV into durable Job Scout storage")
  stage.add_argument("--phone", required=True)
  stage.add_argument("--cv", required=True)
  stage.add_argument("--storage-root", type=Path, default=DEFAULT_STORAGE_ROOT)
  stage.set_defaults(func=cmd_stage_cv)

  profile = subparsers.add_parser("save-profile", help="store WhatsApp-collected Job Scout profile")
  profile.add_argument("--phone", required=True)
  profile.add_argument("--user-id")
  profile.add_argument("--cv", required=True)
  profile.add_argument("--profile-json", help="JSON object, or @path to a JSON file")
  profile.add_argument("--target-role", action="append")
  profile.add_argument("--location", action="append")
  profile.add_argument("--country")
  profile.add_argument("--language")
  profile.add_argument("--qualification", action="append")
  profile.add_argument("--experience", action="append")
  profile.add_argument("--skill", action="append")
  profile.add_argument("--exclusion", action="append")
  profile.add_argument("--prompt-note", action="append")
  profile.add_argument("--max-applications-per-run", type=int)
  profile.add_argument("--no-auto-apply", action="store_true")
  profile.add_argument("--storage-root", type=Path, default=DEFAULT_STORAGE_ROOT)
  profile.add_argument("--dry-run", action="store_true")
  profile.set_defaults(func=cmd_save_profile)

  ready = subparsers.add_parser("list-ready", help="list ready Job Scout subscribers")
  ready.add_argument("--limit", type=int, default=10)
  ready.set_defaults(func=cmd_list_ready)

  apps = subparsers.add_parser("list-applications", help="list a user's recorded applications")
  apps.add_argument("--user-id", required=True)
  apps.set_defaults(func=cmd_list_applications)

  record = subparsers.add_parser("record-application", help="record a Job Scout application outcome")
  record.add_argument("--user-id", required=True)
  record.add_argument("--company", required=True)
  record.add_argument("--role", required=True)
  record.add_argument("--status", choices=["applied", "skipped", "physical_submission", "failed"], required=True)
  record.add_argument("--application-email")
  record.add_argument("--source", default="")
  record.add_argument("--source-url")
  record.add_argument("--notes")
  record.add_argument("--message-id")
  record.add_argument("--closing")
  record.add_argument("--match-reason")
  record.add_argument("--replace", action="store_true")
  record.set_defaults(func=cmd_record_application)

  fetch_candidates = subparsers.add_parser("fetch-candidates", help="fetch normalized JSearch candidates for a profile")
  fetch_candidates.add_argument("--profile-json", help="JSON object, or @path to a JSON file")
  fetch_candidates.add_argument("--target-role", action="append")
  fetch_candidates.add_argument("--location", action="append")
  fetch_candidates.add_argument("--country")
  fetch_candidates.add_argument("--language")
  fetch_candidates.add_argument("--exclusion", action="append")
  fetch_candidates.add_argument("--limit", type=int, default=5)
  fetch_candidates.set_defaults(func=cmd_fetch_candidates)

  dispatch_jsearch = subparsers.add_parser("dispatch-jsearch", help="fetch JSearch candidates for ready subscribers")
  dispatch_jsearch.add_argument("--run-id")
  dispatch_jsearch.add_argument("--limit-ready", type=int, default=10)
  dispatch_jsearch.add_argument("--max-candidates-per-user", type=int, default=5)
  dispatch_jsearch.add_argument("--dispatch-root", type=Path, default=DEFAULT_DISPATCH_ROOT)
  dispatch_jsearch.add_argument("--dry-run", action="store_true")
  dispatch_jsearch.set_defaults(func=cmd_dispatch_jsearch)

  start_dispatch = subparsers.add_parser("start-dispatch", help="start a sanitized Job Scout dispatch marker")
  start_dispatch.add_argument("--run-id", required=True)
  start_dispatch.add_argument("--ready-count", type=int, required=True)
  start_dispatch.add_argument("--dispatch-root", type=Path, default=DEFAULT_DISPATCH_ROOT)
  start_dispatch.set_defaults(func=cmd_start_dispatch)

  dispatch_outcome = subparsers.add_parser("record-dispatch-outcome", help="record one subscriber dispatch outcome")
  dispatch_outcome.add_argument("--run-id", required=True)
  dispatch_outcome.add_argument("--user-id", required=True)
  dispatch_outcome.add_argument("--status", choices=sorted(DISPATCH_STATUSES), required=True)
  dispatch_outcome.add_argument("--applied-count", type=int, default=0)
  dispatch_outcome.add_argument("--physical-count", type=int, default=0)
  dispatch_outcome.add_argument("--dispatch-root", type=Path, default=DEFAULT_DISPATCH_ROOT)
  dispatch_outcome.set_defaults(func=cmd_record_dispatch_outcome)

  finish_dispatch = subparsers.add_parser("finish-dispatch", help="finish a Job Scout dispatch marker with final counts")
  finish_dispatch.add_argument("--run-id", required=True)
  finish_dispatch.add_argument("--ready-count", type=int, required=True)
  finish_dispatch.add_argument("--processed-count", type=int, required=True)
  finish_dispatch.add_argument("--applied-count", type=int, required=True)
  finish_dispatch.add_argument("--physical-count", type=int, required=True)
  finish_dispatch.add_argument("--failed-count", type=int, required=True)
  finish_dispatch.add_argument("--no-matches-count", type=int, required=True)
  finish_dispatch.add_argument("--dispatch-root", type=Path, default=DEFAULT_DISPATCH_ROOT)
  finish_dispatch.set_defaults(func=cmd_finish_dispatch)

  verify_dispatch = subparsers.add_parser("verify-dispatch", help="verify a Job Scout dispatch marker is complete")
  verify_dispatch.add_argument("--run-id", required=True)
  verify_dispatch.add_argument("--dispatch-root", type=Path, default=DEFAULT_DISPATCH_ROOT)
  verify_dispatch.set_defaults(func=cmd_verify_dispatch)

  return parser


def main() -> int:
  parser = build_parser()
  args = parser.parse_args()
  result = args.func(args)
  print(json.dumps(result, indent=2, sort_keys=True))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
