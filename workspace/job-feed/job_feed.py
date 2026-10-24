#!/usr/bin/env python3
"""Post the daily job-advert feed to the WhatsApp jobs groups.

Run through workspace/bin/job-feed. The one command, deliver-group-feed, posts
cached job adverts to a group and (with --refresh-if-stale) rebuilds the cache
from the job boards first.
"""

from __future__ import annotations

from jobfeed.cli import build_parser, main
from jobfeed.commands_group_feed import cmd_deliver_group_feed

__all__ = ["build_parser", "cmd_deliver_group_feed", "main"]


if __name__ == "__main__":
  raise SystemExit(main())
