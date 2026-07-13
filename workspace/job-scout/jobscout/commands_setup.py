"""CLI commands for Job Scout setup, status, profiles, and candidate previews."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from genaie_env import agent_base_url, agent_base_url_source, api_key, endpoint, env_default
from pending_cache import (
  criteria as pending_criteria,
  get_pending_link,
  remove_pending_links,
  store_pending_link,
  utc_now_iso,
)

from . import api
from .constants import DEFAULT_JSEARCH_COUNTRY, DEFAULT_JSEARCH_LANGUAGE, DEFAULT_WEB_APPLY_URL, RUNTIME_ROOT
from .jsearch import fetch_jsearch_candidates, fetch_scraped_candidates, fetch_zimlinks_candidates, filter_candidates, jsearch_provider
from .profile import (
  clean_items,
  load_profile_json,
  preference_code,
  profile_values,
  resolve_preferred_locations,
  resolve_profile_locations,
  search_queries,
)
from .textutil import public_text
from .util import effective_user, normalize_phone, phone_hash, utc_now

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
  result = api.request_json("POST", endpoint("/internal/job-scout/invite"), {"phone": phone, "ttlSeconds": args.ttl_seconds})
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
  cache_path = api.pending_links_path()
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

  runtime_probe = RUNTIME_ROOT / f".runtime-{os.getpid()}.check"
  try:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    runtime_probe.write_text("ok\n", encoding="utf-8")
  except OSError as err:
    errors.append(f"Job Scout runtime root is not writable: {err}")
  finally:
    try:
      runtime_probe.unlink()
    except OSError:
      pass

  browser_ok = None
  if getattr(args, "check_browser", False):
    apply_url = env_default("JOB_SCOUT_WEB_APPLY_URL", DEFAULT_WEB_APPLY_URL)
    parsed = urllib.parse.urlsplit(apply_url)
    health_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/ready", "", ""))
    try:
      with urllib.request.urlopen(health_url, timeout=10) as response:
        browser_ok = response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
      browser_ok = False
    if not browser_ok:
      errors.append("website application worker health check failed")

  subscriber_count = None
  accessible_cv_count = None
  if getattr(args, "check_subscribers", False) and has_api_key:
    try:
      ready = cmd_list_ready(argparse.Namespace(limit=100)).get("subscribers", [])
      ready = ready if isinstance(ready, list) else []
      subscriber_count = len(ready)
      # cvFileRef is an R2 object key now; verify each CV actually exists in the
      # bucket (the GET endpoint 404s on a missing or stale ref) rather than
      # trusting a non-empty ref.
      accessible_cv_count = sum(
        1
        for subscriber in ready
        if api.cv_object_available(str(subscriber.get("userId") or subscriber.get("uid") or subscriber.get("id") or ""))
      )
      if subscriber_count == 0:
        errors.append("no Job Scout subscribers are ready")
      elif accessible_cv_count != subscriber_count:
        errors.append("one or more Job Scout subscribers have no CV in storage; re-upload is required")
    except (SystemExit, RuntimeError) as err:
      errors.append(public_text(err, 240))

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
    "runtimeRoot": str(RUNTIME_ROOT),
    "browserHealthy": browser_ok,
    "subscriberCount": subscriber_count,
    "accessibleCvCount": accessible_cv_count,
    "errors": errors,
  }
  if errors:
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(1)
  return result


def resolve_cv_ref(user_id: str, cv_arg: str, do_upload: bool) -> str:
  """`--cv` may be a local PDF (upload it to R2) or this user's already-staged R2 key."""
  text = str(cv_arg or "").strip()
  if not text:
    raise SystemExit("--cv is required")
  expected_key = f"{user_id}/cv/cv.pdf"
  if text == expected_key:
    return text  # already staged via stage-cv; value is this user's R2 object key
  if Path(text).expanduser().is_file():
    return str(api.upload_cv(user_id, text).get("key") or "") if do_upload else expected_key
  # Not a real local file and not the expected staged key: never persist a bogus ref.
  raise SystemExit(f"CV file not found (and not a staged CV reference): {text}")


def cmd_stage_cv(args: argparse.Namespace) -> dict:
  return api.stage_cv(args.phone, args.cv)


def cmd_save_profile(args: argparse.Namespace) -> dict:
  phone = normalize_phone(args.phone)
  user_id = str(args.user_id or "").strip() or api.linked_user_id(phone)
  profile_confirmed = bool(getattr(args, "profile_confirmed", False))
  safety_acknowledged = bool(getattr(args, "safety_acknowledged", False))
  if not profile_confirmed:
    raise SystemExit("--profile-confirmed is required after the user confirms the profile summary")
  if not safety_acknowledged:
    raise SystemExit("--safety-acknowledged is required after the user acknowledges the scam warning")
  base = load_profile_json(args.profile_json)
  auto_apply = False if args.no_auto_apply else base.get("autoApply", True) is not False
  max_applications = (
    args.max_applications_per_run
    if args.max_applications_per_run is not None
    else base.get("maxApplicationsPerRun", 2)
  )
  resolved_locations = resolve_profile_locations(
    profile_values(base, "locations"),
    args.location,
    args.country,
    base.get("country"),
  )
  preferences = {
    **base,
    "targetRoles": clean_items([*profile_values(base, "targetRoles"), *(args.target_role or [])]),
    "locations": resolved_locations["locations"],
    "country": resolved_locations["country"],
    "language": args.language or base.get("language") or DEFAULT_JSEARCH_LANGUAGE,
    "qualifications": clean_items([*profile_values(base, "qualifications"), *(args.qualification or [])]),
    "experience": clean_items([*profile_values(base, "experience"), *(args.experience or [])]),
    "skills": clean_items([*profile_values(base, "skills"), *(args.skill or [])]),
    "exclusions": clean_items([*profile_values(base, "exclusions"), *(args.exclusion or [])]),
    "promptNotes": clean_items([*profile_values(base, "promptNotes"), *(args.prompt_note or [])]),
    "autoApply": auto_apply,
    "maxApplicationsPerRun": max_applications,
  }
  if args.dry_run:
    return {
      "dryRun": True,
      "phoneHash": phone_hash(phone),
      "userId": user_id,
      "cvFileRef": resolve_cv_ref(user_id, args.cv, do_upload=False),
      "preferences": preferences,
      "profileConfirmed": profile_confirmed,
      "safetyAcknowledged": safety_acknowledged,
      "uploadEndpoint": endpoint("/internal/job-scout/cv"),
      "endpoint": endpoint("/internal/job-scout/profile"),
    }
  cv_file_ref = resolve_cv_ref(user_id, args.cv, do_upload=True)
  payload = {
    "phone": phone,
    "userId": user_id,
    "preferences": preferences,
    "cvFileRef": cv_file_ref,
    "profileConfirmed": profile_confirmed,
    "safetyAcknowledged": safety_acknowledged,
  }
  result = api.request_json("POST", endpoint("/internal/job-scout/profile"), payload)
  remove_pending_links(phone=phone, service="job-scout")
  return result


def cmd_list_ready(args: argparse.Namespace) -> dict:
  query = urllib.parse.urlencode({"limit": args.limit})
  return api.request_json("GET", endpoint(f"/internal/job-scout/subscribers?{query}"))


def cmd_status(args: argparse.Namespace) -> dict:
  """Return requester-scoped readiness from the authoritative backend evaluator."""
  phone = normalize_phone(args.phone)
  query = urllib.parse.urlencode({"phone": phone})
  return api.request_json("GET", endpoint(f"/internal/job-scout/status?{query}"))


def cmd_list_applications(args: argparse.Namespace) -> dict:
  query = urllib.parse.urlencode({"userId": args.user_id})
  return api.request_json("GET", endpoint(f"/internal/job-scout/applications?{query}"))


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
    "submissionMethod": getattr(args, "submission_method", None),
    "applicationUrl": getattr(args, "application_url", None),
    "blockerCode": getattr(args, "blocker_code", None),
    "attemptCount": getattr(args, "attempt_count", None),
    "evidence": getattr(args, "evidence", None),
    "replace": args.replace,
  }
  return api.request_json("POST", endpoint("/internal/job-scout/applications"), payload)


def cmd_fetch_candidates(args: argparse.Namespace) -> dict:
  base = load_profile_json(args.profile_json)
  resolved_locations = resolve_profile_locations(
    profile_values(base, "locations"),
    args.location,
    args.country,
    base.get("country"),
  )
  preferences = {
    **base,
    "targetRoles": clean_items([*profile_values(base, "targetRoles"), *(args.target_role or [])]),
    "locations": resolved_locations["locations"],
    "country": resolved_locations["country"],
    "language": args.language or base.get("language") or DEFAULT_JSEARCH_LANGUAGE,
    "exclusions": clean_items([*profile_values(base, "exclusions"), *(args.exclusion or [])]),
    "maxApplicationsPerRun": args.limit,
  }
  raw_candidates = (
    fetch_zimlinks_candidates(preferences, args.limit)
    + fetch_jsearch_candidates(preferences, args.limit)
    + fetch_scraped_candidates(preferences, args.limit)
  )
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


def cmd_resolve_location(args: argparse.Namespace) -> dict:
  resolved = resolve_preferred_locations(args.location)
  return {"ok": True, **resolved}


