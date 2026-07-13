"""Persistent dispatch marker commands and consistency verification."""

from __future__ import annotations

import argparse
import json

from . import dispatch_store
from .constants import DISPATCH_STATUSES
from .profile import user_key
from .util import count_arg, safe_run_id, utc_now

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

