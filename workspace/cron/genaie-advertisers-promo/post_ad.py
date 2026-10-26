#!/usr/bin/env python3
"""Daily Genaie advertiser recruitment advert on the ads WhatsApp account.

Uses the operator's fixed copy and image. --dry-run and --refs inspect the
campaign without sending or writing state, through the shared broadcast engine.
"""

import os
import sys
from pathlib import Path

LIB_DIR = str(Path(__file__).resolve().parents[2] / "lib")
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)

import promo_broadcast

HERE = os.path.dirname(os.path.abspath(__file__))
TARGETS_FILE = os.environ.get(
    "GENAIE_ADVERTISERS_PROMO_TARGETS", os.path.join(HERE, "targets.json")
)
STATE_FILE = os.environ.get(
    "GENAIE_ADVERTISERS_PROMO_STATE",
    "/home/joseph/.openclaw/runtime/cron-state/genaie-advertisers-promo/state.json",
)
IMAGE = os.environ.get("GENAIE_ADVERTISERS_PROMO_IMAGE") or os.path.join(
    HERE, "assets", "genaie-advertisers.jpg"
)

# Keep the approved wording fixed; only WhatsApp emphasis replaces Markdown.
POST_TEXT = (
    '*ARE YOU AN ADVERTISER LOOKING FOR BUSINESSES TO PROMOTE?*\n'
    '\n'
    '*Turn your advertising skills into income!*\n'
    '\n'
    'GENAIE connects you with businesses willing to pay you a commission for every successful sale you bring them.\n'
    '\n'
    'We also help you *track your referrals and deals so you get your fair share.*\n'
    '\n'
    '✅ Get access to businesses looking for advertisers\n'
    '✅ Earn commissions on successful sales\n'
    '✅ Track your customers, deals, and commissions\n'
    '✅ Work with multiple businesses\n'
    '\n'
    '*JOIN GENAIE TODAY!*\n'
    '*Find businesses. Bring customers. Earn commissions.*'
)
POST_TEXTS = [POST_TEXT]
TRACKING = {"prefix": "GA", "label": "Genaie advertiser recruitment"}


def main():
    return promo_broadcast.broadcast(
        text=POST_TEXTS,
        image=IMAGE,
        targets_file=TARGETS_FILE,
        state_file=STATE_FILE,
        tracking=TRACKING,
        promo_name="genaie-advertisers-promo",
    )


if __name__ == "__main__":
    sys.exit(main())
