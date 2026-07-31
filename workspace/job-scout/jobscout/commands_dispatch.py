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
  if data.get("finishedAt"):
    raise SystemExit("dispatch run is already finished")
  status = args.status
  if status not in DISPATCH_STATUSES:
    raise SystemExit(f"status must be one of: {', '.join(sorted(DISPATCH_STATUSES))}")
  outcomes = data.setdefault("outcomes", {})
  key = user_key(args.user_id)
  existing = outcomes.get(key)
  if isinstance(existing, dict):
    return {
      "ok": True,
      "runId": safe_run_id(args.run_id),
      "duplicate": True,
      "outcomeCount": len(outcomes),
      "outcome": existing,
    }
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
  result["duplicate"] = False
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
  require_no_failures = bool(getattr(args, "require_no_failures", False))
  ready_count = int(data.get("readyCount", -1))
  outcomes = data.get("outcomes") if isinstance(data.get("outcomes"), dict) else {}
  final_counts = data.get("finalCounts") if isinstance(data.get("finalCounts"), dict) else None

  if data.get("phase") == "failed":
    reason = str(data.get("preparationError") or "preparation failed").strip()
    errors.append(f"dispatch preparation failed: {reason}")

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
    if require_no_failures and int(final_counts.get("failedCount", 0)) > 0:
      errors.append("dispatch contains one or more failed subscriber outcomes")

  if require_no_failures:
    summary_failure_count = 0
    claim_ids = data.get("claimIds") if isinstance(data.get("claimIds"), list) else []
    for claim_id in claim_ids:
      try:
        claim = dispatch_store.load_agent_claim(args.dispatch_root, args.run_id, str(claim_id))
      except (OSError, RuntimeError, SystemExit, ValueError):
        errors.append("dispatch is missing a claim required for strict verification")
        continue
      outcome = claim.get("outcome") if isinstance(claim.get("outcome"), dict) else {}
      if str(outcome.get("summaryError") or "").strip():
        summary_failure_count += 1
    if summary_failure_count:
      errors.append("one or more requested WhatsApp summaries failed")

  result = {
    "ok": not errors,
    "runId": safe_run_id(args.run_id),
    "readyCount": ready_count,
    "outcomeCount": len(outcomes),
    "finalCounts": final_counts,
    "strict": require_no_failures,
    "errors": errors,
  }
  if errors:
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(1)
  return result


def cmd_reconcile_agent_state(args: argparse.Namespace) -> dict:
  """Mark a stale active run failed without starting or resending any work."""
  with dispatch_store.agent_state_lock(args.dispatch_root):
    active = dispatch_store.active_agent_dispatch(args.dispatch_root)
    previous_run_id = str((active or {}).get("runId") or "").strip() or None
    active_run_id = dispatch_store.resolve_active_agent_run(args.dispatch_root)
  return {
    "ok": True,
    "previousRunId": previous_run_id,
    "activeRunId": active_run_id,
    "reconciled": previous_run_id is not None and active_run_id is None,
  }
