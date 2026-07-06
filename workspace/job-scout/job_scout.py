#!/usr/bin/env python3
"""Manage Job Scout setup through Agent Genaie's internal API."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from country_codes import country_code, country_name

from jobscout.constants import (
  CV_URL_LOOKUP_ATTEMPTS,
  CV_URL_LOOKUP_RETRY_SECONDS,
  DEFAULT_DISPATCH_ROOT,
  DEFAULT_JSEARCH_COUNTRY,
  DEFAULT_JSEARCH_DATE_POSTED,
  DEFAULT_JSEARCH_LANGUAGE,
  DEFAULT_OPENWEBNINJA_BASE_URL,
  DEFAULT_RAPIDAPI_BASE_URL,
  DEFAULT_RAPIDAPI_HOST,
  DEFAULT_STORAGE_ROOT,
  DEFAULT_WEB_APPLY_URL,
  DISPATCH_STATUSES,
  GENAIE_MAIL_BIN,
  JSEARCH_RETRY_SECONDS,
  MAX_APPLICATION_ATTEMPTS,
  OPENCLAW_BIN_FALLBACKS,
  PUBLIC_DISPATCH_FAILURE_MESSAGE,
  RUNTIME_ROOT,
  TERMINAL_APPLICATION_STATUSES,
  WORKSPACE,
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

# Extracted Job Scout internals. Re-imported here so the CLI/orchestration layer
# calls them by bare name and existing mock.patch("job_scout.<name>") targets keep
# working. Helpers that call each other inside a submodule are patched at their
# defining module (e.g. jobscout.jsearch.jsearch_request).
from jobscout import api
from jobscout import dispatch_store
from jobscout.textutil import public_text
from jobscout.util import (
  count_arg,
  effective_user,
  normalize_phone,
  phone_hash,
  safe_run_id,
  slug_part,
  utc_now,
)
from jobscout.profile import (
  clean_items,
  load_profile_json,
  max_applications,
  normalize_stored_locations,
  preference_code,
  profile_values,
  resolve_preferred_locations,
  resolve_profile_locations,
  resolved_profile_country,
  search_queries,
  subscriber_preferences,
  subscriber_user_id,
  user_key,
)
from jobscout.jsearch import (
  JSearchUnavailable,
  application_key,
  apply_links,
  apply_source_filter_stats,
  candidate_key,
  closing_state,
  extract_emails,
  fetch_candidate_sources,
  fetch_jsearch_candidates,
  fetch_jsearch_candidates_with_result,
  fetch_scraped_candidates,
  fetch_zimlinks_candidates,
  filter_candidates,
  filter_candidates_with_stats,
  finalize_channel_candidate_dispatch,
  jsearch_items,
  jsearch_provider,
  jsearch_request,
  normalized_candidate,
  record_channel_candidate_outcome,
  register_channel_candidate_eligibility,
  zw_targeted,
)


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


def applications_from_response(result: dict) -> list[dict]:
  applications = result.get("applications")
  if isinstance(applications, list):
    return [item for item in applications if isinstance(item, dict)]
  if isinstance(result, list):
    return [item for item in result if isinstance(item, dict)]
  return []


def candidate_attempt(applications: list[dict], candidate: dict) -> tuple[int, bool]:
  key = application_key(candidate)
  for application in applications:
    if application_key(application) != key:
      continue
    try:
      previous = int(application.get("attemptCount") or 0)
    except (TypeError, ValueError):
      previous = 0
    return min(previous + 1, MAX_APPLICATION_ATTEMPTS), True
  return 1, False


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


# cvFileRef is now an R2 object key, not a local path. Downloaded CVs are cached
# per user for the duration of a dispatch run so multiple candidates reuse one
# download.
_cv_download_cache: dict[str, str] = {}


def cv_local_dest(subscriber: dict) -> Path:
  return DEFAULT_STORAGE_ROOT / subscriber_phone_hash(subscriber) / "cv.pdf"


def subscriber_cv_file(subscriber: dict) -> str:
  user_id = subscriber_user_id(subscriber)
  cached = _cv_download_cache.get(user_id)
  if cached and Path(cached).is_file():
    return cached
  if not str(subscriber.get("cvFileRef") or "").strip():
    raise RuntimeError("subscriber is missing cvFileRef")
  path = str(api.download_cv(user_id, cv_local_dest(subscriber)))
  _cv_download_cache[user_id] = path
  return path


def subscriber_phone_hash(subscriber: dict) -> str:
  value = str(subscriber.get("whatsappPhoneHash") or "").strip()
  if value:
    return re.sub(r"[^A-Za-z0-9_.:-]", "-", value)[:80]
  return phone_hash(subscriber_phone(subscriber))


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


def run_json_command(command: list[str], timeout: int = 90, env: dict[str, str] | None = None) -> dict:
  try:
    completed = subprocess.run(
      command,
      check=False,
      capture_output=True,
      text=True,
      timeout=timeout,
      env=env,
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
  command_env = os.environ.copy()
  command_env.pop("OPENCLAW_HOME", None)
  gateway_token = command_env.get("GATEWAY_AUTH_TOKEN", "").strip()
  if gateway_token:
    command_env["OPENCLAW_GATEWAY_TOKEN"] = gateway_token
  return run_json_command([
    str(api.OPENCLAW_BIN),
    "message",
    "send",
    "--channel",
    "whatsapp",
    "--target",
    phone,
    "--message",
    message,
    "--json",
  ], env=command_env)


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


def candidate_application_url(candidate: dict) -> str | None:
  links = candidate.get("applyLinks")
  if isinstance(links, list):
    for value in links:
      link = str(value or "").strip()
      if link.startswith(("https://", "http://")):
        return link
  # The channel permalink is provenance, not an application form.
  if str(candidate.get("source") or "") == "zimlinks-whatsapp":
    return None
  source = str(candidate.get("sourceUrl") or "").strip()
  return source if source.startswith(("https://", "http://")) else None


def applicant_name(subscriber: dict) -> tuple[str, str]:
  parts = display_name(subscriber).split()
  if len(parts) <= 1:
    return (parts[0] if parts else "Applicant", "")
  return parts[0], " ".join(parts[1:])


def web_apply_payload(subscriber: dict, preferences: dict, candidate: dict, cover_letter: Path) -> dict:
  first_name, last_name = applicant_name(subscriber)
  locations = clean_items(profile_values(preferences, "locations"))
  return {
    "attemptId": candidate_key(candidate),
    "applicationUrl": candidate_application_url(candidate),
    "applicant": {
      "firstName": first_name,
      "lastName": last_name,
      "fullName": display_name(subscriber),
      "email": subscriber_sender_email(subscriber),
      "phone": subscriber_phone(subscriber),
      "location": locations[0] if locations else "",
    },
    "cvPath": subscriber_cv_file(subscriber),
    "coverLetterPath": str(cover_letter),
    "autoSubmit": True,
  }


def request_web_application(payload: dict, timeout: int = 180) -> dict:
  url = env_default("JOB_SCOUT_WEB_APPLY_URL", DEFAULT_WEB_APPLY_URL).strip()
  token = env_default("JOB_SCOUT_BROWSER_TOKEN", "").strip()
  if not url:
    raise RuntimeError("website application worker is not configured")
  if not token:
    raise RuntimeError("website application worker token is not configured")
  request = urllib.request.Request(
    url,
    data=json.dumps(payload).encode("utf-8"),
    method="POST",
    headers={
      "Content-Type": "application/json",
      "Accept": "application/json",
      "Authorization": f"Bearer {token}",
    },
  )
  try:
    with urllib.request.urlopen(request, timeout=timeout) as response:
      data = json.loads(response.read().decode("utf-8") or "{}")
  except urllib.error.HTTPError as err:
    raise RuntimeError(f"website application worker returned HTTP {err.code}") from err
  except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as err:
    raise RuntimeError("website application worker is unavailable") from err
  if not isinstance(data, dict) or data.get("outcome") not in {"submitted", "action_required", "failed"}:
    raise RuntimeError("website application worker returned an invalid result")
  return data


def record_candidate_application(
  user_id: str,
  candidate: dict,
  status: str,
  notes: str | None = None,
  message_id: str | None = None,
  match: str | None = None,
  submission_method: str | None = None,
  application_url: str | None = None,
  blocker_code: str | None = None,
  attempt_count: int | None = None,
  evidence: dict | None = None,
  replace: bool = False,
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
    submission_method=submission_method,
    application_url=application_url,
    blocker_code=blocker_code,
    attempt_count=attempt_count,
    evidence=evidence,
    replace=replace,
  ))


def action_instruction(candidate: dict, blocker_code: str | None = None) -> str:
  link = candidate_application_url(candidate) or str(candidate.get("sourcePostUrl") or candidate.get("sourceUrl") or "").strip()
  reasons = {
    "login_required": "The site requires you to sign in.",
    "captcha_required": "The site requires a CAPTCHA.",
    "unknown_required_field": "The form asks a required question I cannot answer safely.",
    "legal_declaration": "The form requires a declaration only you should confirm.",
    "payment_required": "The site asks for payment; I will never submit or pay it.",
    "unsupported_site": "This site's application form is not safely automatable yet.",
    "channel_post_manual": "The channel post has no application form or email. Review the original advert and apply manually.",
  }
  prefix = reasons.get(str(blocker_code or ""), "This application needs your input.")
  if link:
    return f"{prefix} Continue here: {link}"
  return prefix


def build_user_summary(date_label: str, applied: list[dict], action_required: list[dict], failed_note: str | None = None) -> str:
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
  if action_required:
    for item in action_required:
      candidate = item["candidate"] if isinstance(item, dict) and "candidate" in item else item
      blocker_code = item.get("blockerCode") if isinstance(item, dict) else None
      lines.extend([
        f"- {public_text(candidate.get('role'), 100)} - ACTION NEEDED",
        f"Company: {public_text(candidate.get('company'), 100)}",
        action_instruction(candidate, blocker_code),
        "",
      ])
  if failed_note:
    lines.append(PUBLIC_DISPATCH_FAILURE_MESSAGE)
  if not applied and not action_required and not failed_note:
    lines.append("No strong matches today. I will keep checking.")
  return "\n".join(line for line in lines).strip()


def scoped_subscribers(subscribers: list[dict], phone: str | None) -> list[dict]:
  if not phone:
    return subscribers
  target = normalize_phone(phone)
  selected: list[dict] = []
  for subscriber in subscribers:
    try:
      if normalize_phone(subscriber_phone(subscriber)) == target:
        selected.append(subscriber)
    except (RuntimeError, SystemExit):
      continue
  if not selected:
    raise SystemExit("no ready Job Scout subscriber matches that phone")
  return selected


def subscriber_is_dispatch_ready(subscriber: dict) -> bool:
  preferences = subscriber_preferences(subscriber)
  return bool(
    subscriber.get("ready") is True
    and subscriber.get("gmailConnected") is True
    and str(subscriber.get("senderEmail") or "").strip()
    and str(subscriber.get("cvFileRef") or "").strip()
    and str(subscriber.get("whatsappPhone") or "").strip()
    and clean_items(profile_values(preferences, "targetRoles"))
    and clean_items(profile_values(preferences, "locations"))
  )


def cmd_dispatch_jsearch(args: argparse.Namespace) -> dict:
  run_id = args.run_id or f"job-scout-jsearch-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
  run_id = safe_run_id(run_id)
  with dispatch_store.exclusive_dispatch_lock(args.dispatch_root, run_id):
    return dispatch_jsearch_locked(args, run_id)


def dispatch_jsearch_locked(args: argparse.Namespace, run_id: str) -> dict:
  ready_result = cmd_list_ready(argparse.Namespace(limit=args.limit_ready))
  subscribers = ready_result.get("subscribers") if isinstance(ready_result, dict) else []
  if not isinstance(subscribers, list):
    subscribers = []
  subscribers = [subscriber for subscriber in subscribers if subscriber_is_dispatch_ready(subscriber)]
  subscribers = scoped_subscribers(subscribers, getattr(args, "phone", None))
  ready_count = len(subscribers)
  if not args.dry_run:
    cmd_start_dispatch(argparse.Namespace(run_id=run_id, ready_count=ready_count, dispatch_root=args.dispatch_root))

  processed_count = 0
  failed_count = 0
  no_matches_count = 0
  action_required_count = 0
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
    user_action_required: list[dict] = []
    applications: list[dict] = []
    failed_note = None
    source_results = {
      source: {"status": "skipped", "rawCount": 0, "durationMs": 0}
      for source in ("zimlinks-whatsapp", "jsearch", "vacancymail", "iharare", "vacancybox", "applynow")
    }
    if per_user_limit <= 0 or not queries:
      status = "no_matches"
      candidates: list[dict] = []
    else:
      try:
        applications_result = cmd_list_applications(argparse.Namespace(user_id=user_id))
        applications = applications_from_response(applications_result)
        raw_candidates, source_results = fetch_candidate_sources(preferences, per_user_limit)
        candidates, filter_stats = filter_candidates_with_stats(
          raw_candidates,
          preferences,
          applications,
          per_user_limit,
        )
        apply_source_filter_stats(source_results, filter_stats)
        if not args.dry_run:
          register_channel_candidate_eligibility(run_id, user_id, candidates)
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
          "sourceResults": source_results,
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
        attempt_count, replace_existing = candidate_attempt(applications, candidate)
        email = str(candidate.get("applicationEmail") or "").strip()
        application_url = candidate_application_url(candidate)
        if email and auto_apply:
          if args.dry_run:
            user_applied.append({"candidate": candidate, "reason": reason, "method": "email", "dryRun": True})
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
              submission_method="email",
              attempt_count=attempt_count,
              replace=replace_existing,
            )
            record_channel_candidate_outcome(run_id, user_id, candidate, "applied", attempt_count)
            user_applied.append({"candidate": candidate, "reason": reason, "method": "email"})
          except Exception as err:
            failed_note = public_text(err)
            try:
              record_candidate_application(
                user_id,
                candidate,
                "failed",
                notes=failed_note,
                match=reason,
                submission_method="email",
                attempt_count=attempt_count,
                replace=replace_existing,
              )
            except Exception:
              pass
            record_channel_candidate_outcome(run_id, user_id, candidate, "failed", attempt_count)
            break
        elif application_url and auto_apply:
          if args.dry_run:
            user_action_required.append({
              "candidate": candidate,
              "blockerCode": "dry_run",
              "dryRun": True,
            })
            continue
          web_result: dict = {}
          try:
            cover_letter = write_cover_letter(subscriber, preferences, candidate)
            web_result = request_web_application(web_apply_payload(subscriber, preferences, candidate, cover_letter))
            outcome = web_result["outcome"]
            if outcome == "submitted":
              record_candidate_application(
                user_id,
                candidate,
                "applied",
                notes=public_text(web_result.get("confirmation"), 300) or None,
                match=reason,
                submission_method="website",
                application_url=application_url,
                attempt_count=attempt_count,
                replace=replace_existing,
                evidence=web_result.get("evidence") if isinstance(web_result.get("evidence"), dict) else None,
              )
              record_channel_candidate_outcome(run_id, user_id, candidate, "applied", attempt_count)
              user_applied.append({"candidate": candidate, "reason": reason, "method": "website"})
            elif outcome == "action_required":
              blocker_code = str(web_result.get("blockerCode") or "unsupported_site")
              record_candidate_application(
                user_id,
                candidate,
                "action_required",
                notes=action_instruction(candidate, blocker_code),
                match=reason,
                submission_method="website",
                application_url=application_url,
                blocker_code=blocker_code,
                attempt_count=attempt_count,
                replace=replace_existing,
              )
              record_channel_candidate_outcome(run_id, user_id, candidate, "action_required", attempt_count)
              user_action_required.append({"candidate": candidate, "blockerCode": blocker_code})
            else:
              raise RuntimeError(public_text(web_result.get("error") or "website submission failed", 240))
          except Exception as err:
            failed_note = public_text(err)
            try:
              record_candidate_application(
                user_id,
                candidate,
                "failed",
                notes=failed_note,
                match=reason,
                submission_method="website",
                application_url=application_url,
                blocker_code=str(web_result.get("blockerCode") or "worker_failure"),
                attempt_count=attempt_count,
                replace=True,
              )
            except Exception:
              pass
            record_channel_candidate_outcome(run_id, user_id, candidate, "failed", attempt_count)
            break
        elif application_url:
          blocker_code = "auto_apply_disabled"
          if not args.dry_run:
            record_candidate_application(
              user_id,
              candidate,
              "action_required",
              notes=action_instruction(candidate, blocker_code),
              match=reason,
              submission_method="manual",
              application_url=application_url,
              blocker_code=blocker_code,
              attempt_count=0,
            )
            record_channel_candidate_outcome(run_id, user_id, candidate, "action_required", 0)
          user_action_required.append({"candidate": candidate, "blockerCode": blocker_code})
        elif str(candidate.get("source") or "") == "zimlinks-whatsapp":
          blocker_code = "channel_post_manual"
          if not args.dry_run:
            record_candidate_application(
              user_id,
              candidate,
              "action_required",
              notes=action_instruction(candidate, blocker_code),
              match=reason,
              submission_method="manual",
              blocker_code=blocker_code,
              attempt_count=0,
            )
            record_channel_candidate_outcome(run_id, user_id, candidate, "action_required", 0)
          user_action_required.append({"candidate": candidate, "blockerCode": blocker_code})

    if user_applied:
      status = "applied"
      applied_count += len(user_applied)
      action_required_count += len(user_action_required)
    elif user_action_required:
      status = "action_required"
      action_required_count += len(user_action_required)
    elif failed_note:
      status = "failed"
      failed_count += 1
    else:
      status = "no_matches"
      no_matches_count += 1

    if not args.dry_run:
      try:
        send_whatsapp_summary(subscriber_phone(subscriber), build_user_summary(date_label, user_applied, user_action_required, failed_note))
      except Exception as err:
        if status != "failed":
          status = "failed"
          failed_count += 1
          if user_applied:
            applied_count -= len(user_applied)
          if user_action_required:
            action_required_count -= len(user_action_required)
          if not user_applied and not user_action_required:
            no_matches_count -= 1
        failed_note = public_text(err)
        users.append({
          "userKey": user_key(user_id),
          "status": status,
          "candidateCount": len(candidates),
          "appliedCount": len(user_applied),
          "actionRequiredCount": len(user_action_required),
          "sourceResults": source_results,
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
      "schemaVersion": 2,
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
      "actionRequiredCount": len(user_action_required),
      "status": status,
      "sourceResults": source_results,
      "candidates": candidates,
    }
    artifact_path = None if args.dry_run else dispatch_store.write_candidate_artifact(args.dispatch_root, run_id, user_id, artifact)
    users.append({
      "userKey": user_key(user_id),
      "status": status,
      "candidateCount": len(candidates),
      "appliedCount": len(user_applied),
      "actionRequiredCount": len(user_action_required),
      "artifactPath": artifact_path,
      "sourceResults": source_results,
      "error": failed_note,
    })
    if not args.dry_run:
      cmd_record_dispatch_outcome(argparse.Namespace(
        run_id=run_id,
        user_id=user_id,
        status=status,
        applied_count=len(user_applied) if status != "failed" else 0,
        action_required_count=len(user_action_required) if status != "failed" else 0,
        physical_count=0,
        dispatch_root=args.dispatch_root,
      ))

  if not args.dry_run:
    cmd_finish_dispatch(argparse.Namespace(
      run_id=run_id,
      ready_count=ready_count,
      processed_count=processed_count,
      applied_count=applied_count,
      action_required_count=action_required_count,
      physical_count=0,
      failed_count=failed_count,
      no_matches_count=no_matches_count,
      dispatch_root=args.dispatch_root,
    ))
    cmd_verify_dispatch(argparse.Namespace(run_id=run_id, dispatch_root=args.dispatch_root))
    finalize_channel_candidate_dispatch(run_id)

  return {
    "ok": True,
    "dryRun": args.dry_run,
    "scoped": bool(getattr(args, "phone", None)),
    "runId": run_id,
    "readyCount": ready_count,
    "processedCount": processed_count,
    "candidateCount": applied_count + action_required_count,
    "appliedCount": applied_count,
    "actionRequiredCount": action_required_count,
    "physicalCount": 0,
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
  result = dispatch_store.save_dispatch(data, args.dispatch_root)
  result["readyCount"] = ready_count
  return result


def cmd_record_dispatch_outcome(args: argparse.Namespace) -> dict:
  data = dispatch_store.load_dispatch(args.run_id, args.dispatch_root)
  status = args.status
  if status not in DISPATCH_STATUSES:
    raise SystemExit(f"status must be one of: {', '.join(sorted(DISPATCH_STATUSES))}")
  outcomes = data.setdefault("outcomes", {})
  key = user_key(args.user_id)
  outcomes[key] = {
    "status": status,
    "appliedCount": count_arg(args.applied_count, "applied-count"),
    "actionRequiredCount": count_arg(
      getattr(args, "action_required_count", getattr(args, "physical_count", 0)),
      "action-required-count",
    ),
    "recordedAt": utc_now(),
  }
  data["updatedAt"] = utc_now()
  result = dispatch_store.save_dispatch(data, args.dispatch_root)
  result["outcomeCount"] = len(outcomes)
  return result


def cmd_finish_dispatch(args: argparse.Namespace) -> dict:
  data = dispatch_store.load_dispatch(args.run_id, args.dispatch_root)
  counts = {
    "readyCount": count_arg(args.ready_count, "ready-count"),
    "processedCount": count_arg(args.processed_count, "processed-count"),
    "appliedCount": count_arg(args.applied_count, "applied-count"),
    "actionRequiredCount": count_arg(
      getattr(args, "action_required_count", getattr(args, "physical_count", 0)),
      "action-required-count",
    ),
    "failedCount": count_arg(args.failed_count, "failed-count"),
    "noMatchesCount": count_arg(args.no_matches_count, "no-matches-count"),
  }
  data["readyCount"] = counts["readyCount"]
  data["finalCounts"] = counts
  data["finishedAt"] = utc_now()
  data["updatedAt"] = data["finishedAt"]
  result = dispatch_store.save_dispatch(data, args.dispatch_root)
  result["finalCounts"] = counts
  return result


def cmd_verify_dispatch(args: argparse.Namespace) -> dict:
  data = dispatch_store.load_dispatch(args.run_id, args.dispatch_root)
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
    action_required_total = 0
    for outcome in outcomes.values():
      status = outcome.get("status")
      if status not in DISPATCH_STATUSES:
        errors.append("an outcome has an invalid status")
        continue
      outcome_status_counts[status] += 1
      applied_total += int(outcome.get("appliedCount", 0))
      action_required_total += int(outcome.get("actionRequiredCount", outcome.get("physicalCount", 0)))
    if int(final_counts.get("appliedCount", -1)) != applied_total:
      errors.append("appliedCount does not match recorded outcomes")
    if int(final_counts.get("actionRequiredCount", final_counts.get("physicalCount", -1))) != action_required_total:
      errors.append("actionRequiredCount does not match recorded outcomes")
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
  check_runtime.add_argument("--check-browser", action="store_true")
  check_runtime.add_argument("--check-subscribers", action="store_true")
  check_runtime.set_defaults(func=cmd_check_runtime)

  resolve_location = subparsers.add_parser(
    "resolve-location",
    help="validate preferred locations and derive their ISO country code",
  )
  resolve_location.add_argument("--location", action="append", required=True)
  resolve_location.set_defaults(func=cmd_resolve_location)

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
  profile.add_argument("--profile-confirmed", action="store_true", help="user confirmed the final profile summary")
  profile.add_argument("--safety-acknowledged", action="store_true", help="user acknowledged the scam-safety warning")
  profile.add_argument("--storage-root", type=Path, default=DEFAULT_STORAGE_ROOT)
  profile.add_argument("--dry-run", action="store_true")
  profile.set_defaults(func=cmd_save_profile)

  status = subparsers.add_parser("status", help="check one WhatsApp user's Job Scout readiness")
  status.add_argument("--phone", required=True)
  status.set_defaults(func=cmd_status)

  ready = subparsers.add_parser("list-ready", help="list ready Job Scout subscribers")
  ready.add_argument("--limit", type=int, default=100)
  ready.set_defaults(func=cmd_list_ready)

  apps = subparsers.add_parser("list-applications", help="list a user's recorded applications")
  apps.add_argument("--user-id", required=True)
  apps.set_defaults(func=cmd_list_applications)

  record = subparsers.add_parser("record-application", help="record a Job Scout application outcome")
  record.add_argument("--user-id", required=True)
  record.add_argument("--company", required=True)
  record.add_argument("--role", required=True)
  record.add_argument(
    "--status",
    choices=["applied", "skipped", "action_required", "physical_submission", "failed"],
    required=True,
  )
  record.add_argument("--application-email")
  record.add_argument("--source", default="")
  record.add_argument("--source-url")
  record.add_argument("--notes")
  record.add_argument("--message-id")
  record.add_argument("--closing")
  record.add_argument("--match-reason")
  record.add_argument("--submission-method", choices=["email", "website", "manual"])
  record.add_argument("--application-url")
  record.add_argument("--blocker-code")
  record.add_argument("--attempt-count", type=int)
  record.add_argument("--evidence", type=json.loads)
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
  dispatch_jsearch.add_argument("--phone", help="limit this operator-triggered run to one ready WhatsApp subscriber")
  dispatch_jsearch.add_argument("--limit-ready", type=int, default=100)
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
  dispatch_outcome.add_argument("--action-required-count", type=int, default=0)
  dispatch_outcome.add_argument("--dispatch-root", type=Path, default=DEFAULT_DISPATCH_ROOT)
  dispatch_outcome.set_defaults(func=cmd_record_dispatch_outcome)

  finish_dispatch = subparsers.add_parser("finish-dispatch", help="finish a Job Scout dispatch marker with final counts")
  finish_dispatch.add_argument("--run-id", required=True)
  finish_dispatch.add_argument("--ready-count", type=int, required=True)
  finish_dispatch.add_argument("--processed-count", type=int, required=True)
  finish_dispatch.add_argument("--applied-count", type=int, required=True)
  finish_dispatch.add_argument("--action-required-count", type=int, required=True)
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
