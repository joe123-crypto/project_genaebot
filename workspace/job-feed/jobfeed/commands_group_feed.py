"""Job feed delivery command: post cached job adverts to a WhatsApp group."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import math
import os
import random
import time

from . import group_feed
from .constants import (
  DEFAULT_GROUP_FEED_INVITE_URL,
  DEFAULT_GROUP_FEED_LIMIT,
  DEFAULT_GROUP_FEED_SEND_DELAY_SECONDS,
)
from .delivery import send_whatsapp_message
from .textutil import public_text

def _finite_non_negative(value: object, fallback: float) -> float:
  try:
    number = float(value)
  except (TypeError, ValueError):
    return fallback
  if not math.isfinite(number) or number < 0:
    return fallback
  return number


def send_gap_bounds(args: argparse.Namespace) -> tuple[float, float]:
  """Return (min, max) seconds for the random pause between group-feed sends.

  A fixed pause puts the posts on a metronome-regular cadence, which WhatsApp
  spam detection reportedly flags, so each gap is drawn from this range. With
  no max given (or a max at or below the min) the pause is pinned to the min.
  """
  low = _finite_non_negative(
    getattr(args, "send_delay_seconds", DEFAULT_GROUP_FEED_SEND_DELAY_SECONDS),
    DEFAULT_GROUP_FEED_SEND_DELAY_SECONDS,
  )
  high = _finite_non_negative(getattr(args, "send_delay_max_seconds", low), low)
  return low, max(low, high)


def _env(name: str, legacy_name: str, default: str = "") -> str:
  """Read JOB_FEED_* first, then the JOB_SCOUT_GROUP_FEED_* name it replaced."""
  return os.environ.get(name, "") or os.environ.get(legacy_name, "") or default


def group_feed_target_from_args(args: argparse.Namespace) -> str:
  target = str(getattr(args, "target", "") or _env("JOB_FEED_TARGET", "JOB_SCOUT_GROUP_FEED_TARGET")).strip()
  invite_url = group_feed_invite_url_from_args(args)
  if not target:
    raise SystemExit(
      "--target or JOB_FEED_TARGET is required; resolve the group invite "
      f"{invite_url} to a WhatsApp group target ending in @g.us"
    )
  if "chat.whatsapp.com/" in target:
    raise SystemExit("the job feed target must be a WhatsApp group target, not an invite URL")
  return target


def group_feed_invite_url_from_args(args: argparse.Namespace) -> str:
  return str(
    getattr(args, "invite_url", "")
    or _env("JOB_FEED_INVITE_URL", "JOB_SCOUT_GROUP_FEED_INVITE_URL", DEFAULT_GROUP_FEED_INVITE_URL)
  ).strip()


def cmd_deliver_group_feed(args: argparse.Namespace) -> dict:
  target = group_feed_target_from_args(args)
  invite_url = group_feed_invite_url_from_args(args)
  root = args.group_feed_root
  with group_feed.exclusive_delivery_lock(root):
    cache_refreshed = False
    try:
      cache = group_feed.load_latest_cache(root, args.cache_max_age_seconds)
    except SystemExit as err:
      message = str(err)
      refreshable = message.startswith("Job feed cache is missing:") or message.startswith(
        "Job feed cache is stale:"
      )
      if not getattr(args, "refresh_if_stale", False) or not refreshable:
        raise
      refresh_run_id = f"group-feed-refresh-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
      group_feed.refresh_group_feed_cache(
        refresh_run_id,
        root=root,
        limit=getattr(args, "group_feed_limit", DEFAULT_GROUP_FEED_LIMIT),
      )
      cache = group_feed.load_latest_cache(root, args.cache_max_age_seconds)
      cache_refreshed = True
    state = group_feed.load_delivered_state(root)
    # Track delivery per target so the same cached feed can be posted
    # independently to more than one group (e.g. the main jobs group and Hustlers
    # Marketplace); a send to one group must not mark the job delivered for another.
    delivered = group_feed.delivered_keys_for_target(state, target)
    candidates = cache.get("candidates") if isinstance(cache.get("candidates"), list) else []
    pending = [
      candidate
      for candidate in candidates
      if group_feed.candidate_delivery_key(candidate) not in delivered
    ]
    if args.max_messages > 0:
      pending = pending[:args.max_messages]

    delay_min, delay_max = send_gap_bounds(args)
    sent_count = 0
    skipped_count = len(candidates) - len(pending)
    failed_count = 0
    messages: list[dict] = []
    for candidate in pending:
      key = group_feed.candidate_delivery_key(candidate)
      message = group_feed.format_group_feed_message(candidate, invite_url=invite_url)
      if args.dry_run:
        messages.append({
          "key": key,
          "role": public_text(candidate.get("role"), 160),
          "message": message,
        })
        continue
      if delay_max > 0 and sent_count > 0:
        time.sleep(random.uniform(delay_min, delay_max))
      try:
        result = send_whatsapp_message(target, message)
      except Exception as err:
        failed_count += 1
        messages.append({
          "key": key,
          "role": public_text(candidate.get("role"), 160),
          "error": public_text(err, 240),
        })
        break
      delivered[key] = group_feed.delivery_record(candidate, result, target, str(cache.get("runId") or "group-feed"))
      group_feed.save_delivered_state(root, state)
      sent_count += 1
      messages.append({
        "key": key,
        "role": public_text(candidate.get("role"), 160),
        "messageId": delivered[key].get("messageId"),
      })

  return {
    "ok": failed_count == 0,
    "dryRun": args.dry_run,
    "target": target,
    "cacheRunId": cache.get("runId"),
    "cacheCreatedAt": cache.get("createdAt"),
    "cacheRefreshed": cache_refreshed,
    "candidateCount": len(candidates),
    "pendingCount": len(pending),
    "sentCount": sent_count,
    "skippedCount": skipped_count,
    "failedCount": failed_count,
    "messages": messages,
  }

