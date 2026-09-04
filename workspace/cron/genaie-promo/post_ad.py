#!/usr/bin/env python3
"""
Genaie-services daily ad poster.

Posts a FIXED, predefined advert (caption + image) for Genaie's own services
(WhatsApp group advertising, poster design, WhatsApp bots) to a configured set of
WhatsApp groups, once per day per group. There is NO AI/LLM generation involved:
the post text is a hardcoded constant and the image is a bundled asset. This
script is meant to run from an OpenClaw `--command` cron (deterministic, no agent
turn).

Delivery, invite/JID resolution, per-group state, and the permission-error skip
all live in the shared `workspace/lib/promo_broadcast.py` engine (identical across
the cv-promo and building-materials-promo crons); this script only supplies the
advert text, image, and file paths.

Targets come from targets.json (JIDs and/or invite links). Invite links are
resolved to group JIDs via the OpenClaw WhatsApp directory; the bot must already
be a MEMBER of each group for resolution and sending to work. Resolved JIDs are
cached in the state file to avoid re-resolving every day.

Usage:
  post_ad.py            normal run (resolve, skip groups already sent today, send)
  post_ad.py --dry-run  show resolved targets + the message; no send, no state
                        mutation

Std-lib only.
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
    "GENAIE_PROMO_TARGETS", os.path.join(HERE, "targets.json")
)
IMAGE_PATH = os.environ.get(
    "GENAIE_PROMO_IMAGE", os.path.join(HERE, "assets", "genaie-services.png")
)
STATE_FILE = os.environ.get(
    "GENAIE_PROMO_STATE",
    "/home/joseph/.openclaw/runtime/cron-state/genaie-promo/state.json",
)
SEND_DELAY_SECONDS = float(os.environ.get("GENAIE_PROMO_SEND_DELAY", "3"))

# Fixed advert. Verbatim marketing copy supplied by the operator. Emoji are kept
# as explicit \U escapes so the source stays ASCII-safe.
POST_TEXT = (
    "\U0001F4E2 Get Your Business Noticed\n"
    "Get Customers Ready To Buy\n"
    "Get 24/7 Customer Service For Your Business\n"
    "\n"
    "Great products go unnoticed when nobody knows they exist. "
    "Genaie fixes that.\n"
    "\n"
    "\U0001F9F2 Lead Generation\n"
    "\U0001F7E2 WhatsApp Group Ads\n"
    "\U0001F3A8 Poster Design\n"
    "\U0001F916 Customer Service WhatsApp Bots\n"
    "\n"
    "\U0001F4DE WhatsApp us: +263785632943"
)


def main():
    return promo_broadcast.broadcast(
        text=POST_TEXT,
        image=IMAGE_PATH,
        targets_file=TARGETS_FILE,
        state_file=STATE_FILE,
        send_delay=SEND_DELAY_SECONDS,
    )


if __name__ == "__main__":
    sys.exit(main())
