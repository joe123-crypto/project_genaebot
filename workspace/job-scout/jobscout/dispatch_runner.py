"""Per-subscriber Job Scout dispatch orchestration."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from dashboard_telemetry import next_daily_run_iso, publish_task_status

from . import dispatch_store
from .application import (
  action_instruction,
  applications_from_response,
  build_user_summary,
  candidate_application_url,
  candidate_attempt,
  match_reason,
  web_apply_payload,
  write_cover_letter,
)
from .commands_dispatch import (
  cmd_finish_dispatch,
  cmd_record_dispatch_outcome,
  cmd_start_dispatch,
  cmd_verify_dispatch,
)
from .commands_group_feed import maybe_write_group_feed_cache
from .commands_setup import (
  cmd_list_applications,
  cmd_list_ready,
  cmd_record_application,
  convert_pending_cvs_best_effort,
)
from .constants import (
  DEFAULT_JSEARCH_COUNTRY,
  DEFAULT_JSEARCH_LANGUAGE,
  UNKNOWN_EMPLOYER,
  UNKNOWN_ROLE,
  WHATSAPP_CHANNEL_SOURCES,
)
from .delivery import (
  gmail_sender_connected,
  request_web_application,
  result_message_id,
  send_application_email,
  send_whatsapp_summary,
)
from .jsearch import (
  apply_source_filter_stats,
  duplicate_key,
  fetch_candidate_sources,
  filter_candidates_for_user_with_stats,
  finalize_channel_candidate_dispatch,
  global_filter_candidates,
  jsearch_provider,
  record_channel_candidate_outcome,
  register_channel_candidate_eligibility,
)
from .profile import clean_items, max_applications, preference_code, profile_values, search_queries, subscriber_preferences, subscriber_user_id, user_key
from .subscriber import subscriber_cv_html_ready, subscriber_has_whatsapp, subscriber_phone
from .textutil import public_text
from .util import normalize_phone, safe_run_id, utc_now

# Matches the OpenClaw agent-turn cron installed by deploy/install-job-scout.sh.
JOB_SCOUT_SCHEDULE_LABEL = "Daily - 8:00 AM"
JOB_SCOUT_TIMEZONE = "Africa/Harare"


def publish_user_dashboard_telemetry(
  user_id: str,
  status: str,
  applied_count: int,
  action_required_count: int,
  delivered: bool | None,
  error_note: str | None,
) -> None:
  """Best-effort dashboard snapshots after one subscriber's dispatch."""
  if status == "applied":
    last_run_status = "success"
    summary = f"{applied_count} application(s) sent"
    if action_required_count:
      summary += f", {action_required_count} need your action"
  elif status == "action_required":
    last_run_status = "action_required"
    summary = f"{action_required_count} job(s) need your action"
  elif status == "pending_approval":
    last_run_status = "action_required"
    summary = f"{action_required_count} matched job(s) waiting for approval"
  elif status == "failed":
    last_run_status = "failed"
    summary = str(error_note or "")[:240] or "Job Scout run failed"
  else:
    last_run_status = "success"
    summary = "No new matching jobs"

  now = utc_now()
  publish_task_status(
    user_id=user_id,
    task_id="search_apply_jobs",
    service="job_scout",
    status="active",
    schedule_label=JOB_SCOUT_SCHEDULE_LABEL,
    timezone_name=JOB_SCOUT_TIMEZONE,
    next_run_at=next_daily_run_iso(8, 0, JOB_SCOUT_TIMEZONE),
    last_run_at=now,
    last_run_status=last_run_status,
    last_run_summary=summary,
  )
  if delivered is not None:
    publish_task_status(
      user_id=user_id,
      task_id="deliver_results",
      service="delivery",
      status="active",
      schedule_label="After each task run",
      last_run_at=now,
      last_run_status="success" if delivered else "failed",
      last_run_summary="Job Scout summary delivered" if delivered else "WhatsApp delivery failed",
    )


def browser_apply_enabled(args: argparse.Namespace) -> bool:
  override = getattr(args, "browser_apply", None)
  if override is not None:
    return bool(override)
  value = str(os.environ.get("JOB_SCOUT_BROWSER_APPLY_ENABLED", "")).strip().casefold()
  return value in {"1", "true", "yes", "on"}


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
  candidate_snapshot: dict | None = None,
  replace: bool = False,
) -> dict:
  return cmd_record_application(argparse.Namespace(
    user_id=user_id,
    company=str(candidate.get("company") or UNKNOWN_EMPLOYER),
    role=str(candidate.get("role") or UNKNOWN_ROLE),
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
    candidate_snapshot=candidate_snapshot if candidate_snapshot is not None else candidate,
    replace=replace,
  ))


def scoped_subscribers(
  subscribers: list[dict],
  phone: str | None = None,
  user_id: str | None = None,
) -> list[dict]:
  if phone and user_id:
    raise SystemExit("--phone and --user-id cannot be combined")
  if user_id:
    target_user_id = str(user_id).strip()
    selected = [
      subscriber
      for subscriber in subscribers
      if subscriber_user_id(subscriber) == target_user_id
    ]
    if not selected:
      raise SystemExit("no ready Job Scout subscriber matches that user-id")
    return selected
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
    and subscriber_cv_html_ready(subscriber)
    and clean_items(profile_values(preferences, "targetRoles"))
    and clean_items(profile_values(preferences, "locations"))
  )


def candidates_for_subscriber_channels(subscriber: dict, candidates: list[dict]) -> list[dict]:
  if subscriber_has_whatsapp(subscriber):
    return candidates
  return [
    candidate
    for candidate in candidates
    if str(candidate.get("applicationEmail") or "").strip()
  ]


def latest_dispatch_marker(root: Path) -> dict | None:
  """Newest run marker in the dispatch root, or None when no run has recorded one."""
  markers: list[dict] = []
  for path in Path(root).glob("*.json"):
    if path.name == "active-agent-dispatch.json":
      continue
    try:
      data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
      continue
    if isinstance(data, dict) and data.get("runId"):
      markers.append(data)
  if not markers:
    return None
  return max(markers, key=lambda marker: str(marker.get("startedAt") or ""))


def cmd_summary(args: argparse.Namespace) -> dict:
  """One authoritative snapshot: live ready count plus the latest dispatch outcome.

  The live query and the marker half fail independently so an unreachable
  backend still yields last-run stats (and vice versa) instead of a guess.
  """
  ready_count: int | None = None
  ready_user_keys: list[str] = []
  ready_error: str | None = None
  try:
    ready_result = cmd_list_ready(argparse.Namespace(limit=args.limit))
    subscribers = ready_result.get("subscribers") if isinstance(ready_result, dict) else []
    if not isinstance(subscribers, list):
      subscribers = []
    subscribers = [subscriber for subscriber in subscribers if subscriber_is_dispatch_ready(subscriber)]
    ready_count = len(subscribers)
    ready_user_keys = sorted(user_key(subscriber_user_id(subscriber)) for subscriber in subscribers)
  except (RuntimeError, SystemExit, OSError, ValueError) as err:
    ready_error = str(err) or "ready-subscriber query failed"

  marker = latest_dispatch_marker(args.dispatch_root)
  last_run = None
  if marker is not None:
    last_run = {
      "runId": marker.get("runId"),
      "startedAt": marker.get("startedAt"),
      "finishedAt": marker.get("finishedAt"),
      "phase": marker.get("phase"),
      "preparationError": marker.get("preparationError"),
      "finalCounts": marker.get("finalCounts"),
    }

  result: dict = {
    "ok": ready_error is None,
    "readyCount": ready_count,
    "readyUserKeys": ready_user_keys,
    "lastRun": last_run,
  }
  if ready_error is not None:
    result["readyCountError"] = ready_error
  return result


def cmd_dispatch_jsearch(args: argparse.Namespace) -> dict:
  agent_phase = str(getattr(args, "agent_phase", "") or "").strip()
  if agent_phase:
    from .agent_dispatch import cmd_agent_dispatch
    return cmd_agent_dispatch(args)
  run_id = args.run_id or f"job-scout-jsearch-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
  run_id = safe_run_id(run_id)
  dispatch_store.assert_no_active_agent_dispatch(args.dispatch_root)
  with dispatch_store.exclusive_dispatch_lock(args.dispatch_root, run_id):
    return dispatch_jsearch_locked(args, run_id)


def dispatch_jsearch_locked(args: argparse.Namespace, run_id: str) -> dict:
  # Convert any pending PDF CVs first so those subscribers are ready for this
  # very run instead of waiting a day; failures never block dispatch.
  if not args.dry_run:
    convert_pending_cvs_best_effort(args.limit_ready)
  ready_result = cmd_list_ready(argparse.Namespace(limit=args.limit_ready))
  subscribers = ready_result.get("subscribers") if isinstance(ready_result, dict) else []
  if not isinstance(subscribers, list):
    subscribers = []
  subscribers = [subscriber for subscriber in subscribers if subscriber_is_dispatch_ready(subscriber)]
  subscribers = scoped_subscribers(
    subscribers,
    getattr(args, "phone", None),
    getattr(args, "user_id", None),
  )
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
  allow_browser_apply = browser_apply_enabled(args)
  group_feed_cache = maybe_write_group_feed_cache(args, run_id)

  prepared_users: list[dict] = []
  raw_pool: list[dict] = []
  for subscriber in subscribers:
    processed_count += 1
    user_id = subscriber_user_id(subscriber)
    preferences = subscriber_preferences(subscriber)
    per_user_limit = min(args.max_candidates_per_user, max_applications(preferences))
    queries = search_queries(preferences)
    source_results = {
      source: {"status": "skipped", "rawCount": 0, "durationMs": 0}
      for source in (*WHATSAPP_CHANNEL_SOURCES, "jsearch", "vacancymail", "iharare", "vacancybox", "applynow")
    }
    if per_user_limit <= 0 or not queries:
      prepared_users.append({
        "subscriber": subscriber,
        "userId": user_id,
        "preferences": preferences,
        "perUserLimit": per_user_limit,
        "queries": queries,
        "applications": [],
        "rawKeys": [],
        "sourceResults": source_results,
        "status": "no_matches",
        "error": None,
      })
    else:
      try:
        applications_result = cmd_list_applications(argparse.Namespace(user_id=user_id))
        applications = applications_from_response(applications_result)
        raw_candidates, source_results = fetch_candidate_sources(preferences, per_user_limit)
        raw_keys = [duplicate_key(candidate) for candidate in raw_candidates]
        raw_pool.extend(raw_candidates)
        prepared_users.append({
          "subscriber": subscriber,
          "userId": user_id,
          "preferences": preferences,
          "perUserLimit": per_user_limit,
          "queries": queries,
          "applications": applications,
          "rawKeys": raw_keys,
          "sourceResults": source_results,
          "status": None,
          "error": None,
        })
      except SystemExit:
        raise
      except Exception as err:
        prepared_users.append({
          "subscriber": subscriber,
          "userId": user_id,
          "preferences": preferences,
          "perUserLimit": per_user_limit,
          "queries": queries,
          "applications": [],
          "rawKeys": [],
          "sourceResults": source_results,
          "status": "failed",
          "error": public_text(err),
        })

  cleaned_pool, global_filter_stats, global_candidates = global_filter_candidates(raw_pool)
  global_filter_summary = {
    "rawCount": len(raw_pool),
    "candidateCount": len(cleaned_pool),
    "expiredCount": sum(int(stats.get("expiredCount") or 0) for stats in global_filter_stats.values()),
    "duplicateCount": sum(int(stats.get("duplicateCount") or 0) for stats in global_filter_stats.values()),
    "unactionableCount": sum(int(stats.get("unactionableCount") or 0) for stats in global_filter_stats.values()),
    "lowInfoCount": sum(int(stats.get("lowInfoCount") or 0) for stats in global_filter_stats.values()),
    "missingTitleCount": sum(int(stats.get("missingTitleCount") or 0) for stats in global_filter_stats.values()),
    "sourceResults": global_filter_stats,
  }

  for prepared in prepared_users:
    subscriber = prepared["subscriber"]
    user_id = prepared["userId"]
    preferences = prepared["preferences"]
    per_user_limit = prepared["perUserLimit"]
    queries = prepared["queries"]
    applications = prepared["applications"]
    source_results = prepared["sourceResults"]
    auto_apply = preferences.get("autoApply", True) is not False
    user_applied: list[dict] = []
    user_action_required: list[dict] = []
    failed_note = prepared.get("error")
    candidates: list[dict] = []
    prepared_status = prepared.get("status")

    if prepared_status == "failed":
      failed_count += 1
      users.append({
        "userKey": user_key(user_id),
        "status": "failed",
        "sourceResults": source_results,
        "globalFilter": global_filter_summary,
        "error": failed_note,
      })
      if not args.dry_run:
        delivered = None
        if subscriber_has_whatsapp(subscriber):
          delivered = False
          try:
            send_whatsapp_summary(subscriber_phone(subscriber), build_user_summary(date_label, [], [], failed_note))
            delivered = True
          except Exception:
            pass
        cmd_record_dispatch_outcome(argparse.Namespace(
          run_id=run_id,
          user_id=user_id,
          status="failed",
          applied_count=0,
          physical_count=0,
          dispatch_root=args.dispatch_root,
        ))
        publish_user_dashboard_telemetry(user_id, "failed", 0, 0, delivered, failed_note)
      continue

    if prepared_status == "no_matches":
      status = "no_matches"
    else:
      candidate_pool = []
      seen_keys: set[tuple[str, str]] = set()
      for key in prepared.get("rawKeys") or []:
        if key in seen_keys:
          continue
        seen_keys.add(key)
        candidate = global_candidates.get(key)
        if candidate is not None:
          candidate_pool.append(candidate)
      candidates, filter_stats = filter_candidates_for_user_with_stats(
        candidate_pool,
        preferences,
        applications,
        per_user_limit,
      )
      candidates = candidates_for_subscriber_channels(subscriber, candidates)
      apply_source_filter_stats(source_results, filter_stats)
      if not args.dry_run:
        register_channel_candidate_eligibility(run_id, user_id, candidates)

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
          if not gmail_sender_connected(subscriber):
            blocker_code = "gmail_not_connected"
            record_candidate_application(
              user_id,
              candidate,
              "action_required",
              notes=action_instruction(candidate, blocker_code),
              match=reason,
              submission_method="manual",
              application_url=application_url,
              blocker_code=blocker_code,
              attempt_count=attempt_count,
              replace=replace_existing,
            )
            record_channel_candidate_outcome(run_id, user_id, candidate, "action_required", attempt_count)
            user_action_required.append({"candidate": candidate, "blockerCode": blocker_code})
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
        elif application_url and auto_apply and allow_browser_apply:
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
        elif candidate.get("sourceChannelJid") and candidate.get("sourcePostId"):
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

    if not args.dry_run and subscriber_has_whatsapp(subscriber):
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
          "globalFilter": global_filter_summary,
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
          publish_user_dashboard_telemetry(user_id, status, 0, 0, False, failed_note)
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
      "globalFilter": global_filter_summary,
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
      "globalFilter": global_filter_summary,
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
      publish_user_dashboard_telemetry(
        user_id,
        status,
        len(user_applied),
        len(user_action_required),
        True if subscriber_has_whatsapp(subscriber) else None,
        failed_note,
      )

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
    "globalFilter": global_filter_summary,
    "groupFeedCache": group_feed_cache,
    "users": users,
  }
