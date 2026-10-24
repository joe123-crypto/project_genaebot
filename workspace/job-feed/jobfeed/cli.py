"""Argument parser and command-line execution for the job feed."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .commands_group_feed import cmd_deliver_group_feed
from .constants import (
  DEFAULT_GROUP_FEED_CACHE_MAX_AGE_SECONDS,
  DEFAULT_GROUP_FEED_LIMIT,
  DEFAULT_GROUP_FEED_MAX_MESSAGES,
  DEFAULT_GROUP_FEED_ROOT,
  DEFAULT_GROUP_FEED_SEND_DELAY_MAX_SECONDS,
  DEFAULT_GROUP_FEED_SEND_DELAY_SECONDS,
)


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(prog="job-feed")
  subparsers = parser.add_subparsers(dest="command", required=True)

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

  return parser


def main() -> int:
  parser = build_parser()
  args = parser.parse_args()
  result = args.func(args)
  print(json.dumps(result, indent=2, sort_keys=True))
  return 0
