"""Argument parser and command-line execution for Job Scout."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .commands_dispatch import (
  cmd_finish_dispatch,
  cmd_reconcile_agent_state,
  cmd_record_dispatch_outcome,
  cmd_start_dispatch,
  cmd_verify_dispatch,
)
from .commands_group_feed import cmd_deliver_group_feed
from .commands_setup import (
  cmd_build_cv,
  cmd_check_runtime,
  cmd_count_approved_applications,
  cmd_convert_pending_cvs,
  cmd_fetch_candidates,
  cmd_list_applications,
  cmd_list_ready,
  cmd_record_application,
  cmd_repair_cv,
  cmd_resolve_location,
  cmd_save_profile,
  cmd_stage_cv,
  cmd_status,
)
from .constants import (
  DEFAULT_AGENT_MATCH_THRESHOLD,
  DEFAULT_DISPATCH_ROOT,
  DEFAULT_GROUP_FEED_CACHE_MAX_AGE_SECONDS,
  DEFAULT_GROUP_FEED_LIMIT,
  DEFAULT_GROUP_FEED_MAX_MESSAGES,
  DEFAULT_GROUP_FEED_ROOT,
  DEFAULT_GROUP_FEED_SEND_DELAY_SECONDS,
  DEFAULT_GROUP_FEED_SEND_DELAY_MAX_SECONDS,
  DEFAULT_STORAGE_ROOT,
  DISPATCH_STATUSES,
)
from .dispatch_runner import cmd_dispatch_jsearch, cmd_summary

def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(prog="job-scout")
  subparsers = parser.add_subparsers(dest="command", required=True)

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

  build_cv = subparsers.add_parser(
    "build-cv",
    help="build a canonical HTML CV from interview facts and upload it to R2",
  )
  build_cv.add_argument("--phone", required=True)
  build_cv.add_argument("--user-id", default=None, help="Firebase UID; derived from --phone when omitted")
  build_cv.add_argument(
    "--facts",
    required=True,
    help="interview facts as an inline JSON object or @path to a JSON file",
  )
  build_cv.add_argument("--dry-run", action="store_true", help="return the built HTML without uploading")
  build_cv.set_defaults(func=cmd_build_cv)

  profile = subparsers.add_parser("save-profile", help="store WhatsApp-collected Job Scout profile")
  profile.add_argument("--phone", required=True)
  profile.add_argument("--user-id")
  profile.add_argument("--cv", default=None, help="R2 object key from stage-cv, or local CV path; may be omitted when the user already has a CV in R2")
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

  summary = subparsers.add_parser("summary", help="report the current ready count and the latest dispatch outcome")
  summary.add_argument("--limit", type=int, default=100)
  summary.add_argument("--dispatch-root", type=Path, default=DEFAULT_DISPATCH_ROOT)
  summary.set_defaults(func=cmd_summary)

  reconcile = subparsers.add_parser("reconcile-agent-state", help="fail and release a stale active agent run")
  reconcile.add_argument("--dispatch-root", type=Path, default=DEFAULT_DISPATCH_ROOT)
  reconcile.set_defaults(func=cmd_reconcile_agent_state)

  convert = subparsers.add_parser("convert-pending-cvs", help="convert pending PDF CVs into canonical HTML CVs")
  convert.add_argument("--limit", type=int, default=100)
  convert.add_argument("--user-id", action="append", help="convert only these user ids")
  convert.add_argument("--dry-run", action="store_true")
  convert.set_defaults(func=cmd_convert_pending_cvs)

  repair = subparsers.add_parser("repair-cv", help="validate and replace one canonical HTML CV with a reversible backup")
  repair.add_argument("--phone", required=True)
  repair.add_argument("--source-html", required=True)
  repair.add_argument("--backup-dir")
  repair.add_argument("--apply", action="store_true", help="upload after validation; default is dry-run")
  repair.set_defaults(func=cmd_repair_cv)

  apps = subparsers.add_parser("list-applications", help="list a user's recorded applications")
  apps.add_argument("--user-id", required=True)
  apps.set_defaults(func=cmd_list_applications)

  approved = subparsers.add_parser("count-approved-applications", help="count applications queued after user approval")
  approved.add_argument("--limit", type=int, default=100)
  approved.set_defaults(func=cmd_count_approved_applications)

  record = subparsers.add_parser("record-application", help="record a Job Scout application outcome")
  record.add_argument("--user-id", required=True)
  record.add_argument("--company", required=True)
  record.add_argument("--role", required=True)
  record.add_argument(
    "--status",
    choices=["applied", "pending_approval", "approved", "skipped", "action_required", "physical_submission", "failed"],
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
  record.add_argument("--candidate-snapshot", type=json.loads)
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
  dispatch_jsearch.add_argument(
    "--agent-phase",
    choices=["prepare", "claim", "validate", "complete", "fallback", "status", "wait", "finish"],
    help="run one durable OpenClaw-agent orchestration phase",
  )
  dispatch_jsearch.add_argument("--run-id")
  dispatch_jsearch.add_argument("--claim-id", help="opaque claim returned by the agent prepare phase")
  dispatch_jsearch.add_argument("--result", help="agent result JSON object, or @path, for the complete phase")
  dispatch_jsearch.add_argument("--fallback-reason", help="sanitized reason for deterministic fallback")
  dispatch_jsearch.add_argument("--after-terminal-count", type=int, help="wait until terminal claims exceed this count")
  dispatch_jsearch.add_argument("--wait-timeout-seconds", type=int, default=2760, help="bounded wait phase timeout")
  dispatch_scope = dispatch_jsearch.add_mutually_exclusive_group()
  dispatch_scope.add_argument("--phone", help="limit this operator-triggered run to one ready WhatsApp subscriber")
  dispatch_scope.add_argument("--user-id", help="limit this operator-triggered run to one ready Firebase user")
  dispatch_jsearch.add_argument("--limit-ready", type=int, default=100)
  dispatch_jsearch.add_argument("--max-candidates-per-user", type=int, default=5)
  dispatch_jsearch.add_argument("--shortlist-limit", type=int, default=20, help="agent shortlist size, capped at 20")
  dispatch_jsearch.add_argument(
    "--match-threshold",
    type=int,
    default=DEFAULT_AGENT_MATCH_THRESHOLD,
    help="minimum agent fit score for selection",
  )
  dispatch_jsearch.add_argument("--dispatch-root", type=Path, default=DEFAULT_DISPATCH_ROOT)
  dispatch_jsearch.add_argument("--dry-run", action="store_true")
  dispatch_jsearch.add_argument("--write-group-feed-cache", action="store_true")
  dispatch_jsearch.add_argument(
    "--approved-only",
    action="store_true",
    help="process only application records that the user has approved",
  )
  dispatch_jsearch.add_argument("--group-feed-root", type=Path, default=DEFAULT_GROUP_FEED_ROOT)
  dispatch_jsearch.add_argument("--group-feed-limit", type=int, default=DEFAULT_GROUP_FEED_LIMIT)
  dispatch_jsearch.add_argument(
    "--browser-apply",
    action="store_true",
    help="enable website/browser auto-submission for candidates without an application email",
  )
  dispatch_jsearch.set_defaults(func=cmd_dispatch_jsearch)

  deliver_group_feed = subparsers.add_parser("deliver-group-feed", help="send cached broad job adverts to a WhatsApp group")
  deliver_group_feed.add_argument("--target", help="WhatsApp group target, for example 120...@g.us")
  deliver_group_feed.add_argument("--invite-url")
  deliver_group_feed.add_argument("--group-feed-root", type=Path, default=DEFAULT_GROUP_FEED_ROOT)
  deliver_group_feed.add_argument("--cache-max-age-seconds", type=int, default=DEFAULT_GROUP_FEED_CACHE_MAX_AGE_SECONDS)
  deliver_group_feed.add_argument("--refresh-if-stale", action="store_true")
  deliver_group_feed.add_argument("--group-feed-limit", type=int, default=DEFAULT_GROUP_FEED_LIMIT)
  deliver_group_feed.add_argument("--max-messages", type=int, default=DEFAULT_GROUP_FEED_MAX_MESSAGES, help="0 means no cap")
  deliver_group_feed.add_argument(
    "--send-delay-seconds",
    type=float,
    default=DEFAULT_GROUP_FEED_SEND_DELAY_SECONDS,
    help="shortest random pause between group sends",
  )
  deliver_group_feed.add_argument(
    "--send-delay-max-seconds",
    type=float,
    default=DEFAULT_GROUP_FEED_SEND_DELAY_MAX_SECONDS,
    help="longest random pause between group sends (at or below --send-delay-seconds pins the pause)",
  )
  deliver_group_feed.add_argument("--dry-run", action="store_true")
  deliver_group_feed.set_defaults(func=cmd_deliver_group_feed)

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
  verify_dispatch.add_argument(
    "--require-no-failures",
    action="store_true",
    help="also fail when a subscriber outcome or requested WhatsApp summary failed",
  )
  verify_dispatch.set_defaults(func=cmd_verify_dispatch)

  return parser


def main() -> int:
  parser = build_parser()
  args = parser.parse_args()
  if args.command == "dispatch-jsearch" and not args.agent_phase and not args.dry_run:
    raise SystemExit(
      "direct live dispatch is disabled; use job-scout-agent --phone E164 --live "
      "or an --agent-phase run"
    )
  result = args.func(args)
  print(json.dumps(result, indent=2, sort_keys=True))
  return 0
