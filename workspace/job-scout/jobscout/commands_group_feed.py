"""Broad Job Scout group-feed caching and WhatsApp delivery commands."""

from __future__ import annotations

import argparse
import os
import time

from . import group_feed
from .constants import (
  DEFAULT_GROUP_FEED_INVITE_URL,
  DEFAULT_GROUP_FEED_LIMIT,
  DEFAULT_GROUP_FEED_ROOT,
  DEFAULT_GROUP_FEED_SEND_DELAY_SECONDS,
)
from .delivery import send_whatsapp_message
from .textutil import public_text

def maybe_write_group_feed_cache(args: argparse.Namespace, run_id: str) -> dict | None:
  if not getattr(args, "write_group_feed_cache", False):
    return None
  try:
    return group_feed.build_group_feed_cache(
      run_id,
      root=getattr(args, "group_feed_root", DEFAULT_GROUP_FEED_ROOT),
      limit=getattr(args, "group_feed_limit", DEFAULT_GROUP_FEED_LIMIT),
      write=not args.dry_run,
    )
  except Exception as err:
    return {
      "ok": False,
      "error": public_text(err, 240),
      "path": str(group_feed.latest_cache_path(getattr(args, "group_feed_root", DEFAULT_GROUP_FEED_ROOT))),
    }


def group_feed_target_from_args(args: argparse.Namespace) -> str:
  target = str(getattr(args, "target", "") or os.environ.get("JOB_SCOUT_GROUP_FEED_TARGET", "")).strip()
  invite_url = group_feed_invite_url_from_args(args)
  if not target:
    raise SystemExit(
      "JOB_SCOUT_GROUP_FEED_TARGET is required; resolve the group invite "
      f"{invite_url} to a WhatsApp group target ending in @g.us"
    )
  if "chat.whatsapp.com/" in target:
    raise SystemExit("JOB_SCOUT_GROUP_FEED_TARGET must be a WhatsApp group target, not an invite URL")
  return target


def group_feed_invite_url_from_args(args: argparse.Namespace) -> str:
  return str(
    getattr(args, "invite_url", "")
    or os.environ.get("JOB_SCOUT_GROUP_FEED_INVITE_URL", DEFAULT_GROUP_FEED_INVITE_URL)
  ).strip()


def cmd_deliver_group_feed(args: argparse.Namespace) -> dict:
  target = group_feed_target_from_args(args)
  invite_url = group_feed_invite_url_from_args(args)
  root = args.group_feed_root
  with group_feed.exclusive_delivery_lock(root):
    cache = group_feed.load_latest_cache(root, args.cache_max_age_seconds)
    state = group_feed.load_delivered_state(root)
    delivered = state.setdefault("delivered", {})
    candidates = cache.get("candidates") if isinstance(cache.get("candidates"), list) else []
    pending = [
      candidate
      for candidate in candidates
      if group_feed.candidate_delivery_key(candidate) not in delivered
    ]
    if args.max_messages > 0:
      pending = pending[:args.max_messages]

    delay = max(0.0, float(getattr(args, "send_delay_seconds", DEFAULT_GROUP_FEED_SEND_DELAY_SECONDS)))
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
      if delay > 0 and sent_count > 0:
        time.sleep(delay)
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
    "candidateCount": len(candidates),
    "pendingCount": len(pending),
    "sentCount": sent_count,
    "skippedCount": skipped_count,
    "failedCount": failed_count,
    "messages": messages,
  }


