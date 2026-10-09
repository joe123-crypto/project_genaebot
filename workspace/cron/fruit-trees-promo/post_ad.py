#!/usr/bin/env python3
"""
Fruit-trees daily ad poster.

Posts a FIXED, predefined advert (caption + image) for the fruit-tree nursery
(grafted mango, pawpaw, orange, naartjie, lemon) to a configured set of WhatsApp
groups, once per day per group. There is NO AI/LLM generation involved: the post
text is a hardcoded constant and the image is a bundled asset. This script is
meant to run from an OpenClaw `--command` cron (deterministic, no agent turn).

Delivery, invite/JID resolution, per-group state, and the permission-error skip
all live in the shared `workspace/lib/promo_broadcast.py` engine (identical across
the cv-promo, genaie-promo and building-materials-promo crons); this script only
supplies the advert text, image, and file paths.

Targets come from targets.json (JIDs and/or invite links). Invite links are
resolved to group JIDs via the OpenClaw WhatsApp directory; the bot must already
be a MEMBER of each group for resolution and sending to work. Resolved JIDs are
cached in the state file to avoid re-resolving every day.

Usage:
  post_ad.py            normal run (resolve, skip groups already sent today, send)
  post_ad.py --dry-run  show resolved targets, each group's caption variant + ref,
                        and every caption variant; no send, no state
                        mutation
  post_ad.py --refs     print ref code -> promo + group JID + link (decode refs);
                        no send, no state mutation

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
    "FRUIT_TREES_TARGETS", os.path.join(HERE, "targets.json")
)
IMAGE_PATH = os.environ.get(
    "FRUIT_TREES_IMAGE", os.path.join(HERE, "assets", "fruit-trees-ad.jpg")
)
STATE_FILE = os.environ.get(
    "FRUIT_TREES_STATE",
    "/home/joseph/.openclaw/runtime/cron-state/fruit-trees-promo/state.json",
)
SEND_DELAY_SECONDS = float(os.environ.get("FRUIT_TREES_SEND_DELAY", "3"))

# Fixed advert. Verbatim marketing copy supplied by the operator. Emoji are kept
# as explicit \U escapes so the source stays ASCII-safe.
POST_TEXT = (
    "\U0001F333 GRAFTED FRUIT TREES FOR SALE \U0001F333\n"
    "\n"
    "Start your own orchard with strong, healthy grafted fruit trees, "
    "ready to plant:\n"
    "\n"
    "\U0001F96D Mango\n"
    "\U0001F348 Pawpaw\n"
    "\U0001F34A Orange\n"
    "\U0001F34A Naartjie\n"
    "\U0001F34B Lemon\n"
    "\n"
    "\U0001F4B5 Retail: $5 per tree\n"
    "\U0001F4E6 Wholesale: $3 per tree\n"
    "\U0001F69A Delivery available\n"
    "\n"
    "Grafted trees fruit faster and stay true to type. "
    "Plant once, enjoy for years! \U0001F331\n"
    "\n"
    "\U0001F4F2 Order now: +263785632943"
)

# Caption variants that rotate per group per day (see promo_broadcast) so the ad
# doesn't read as copy-paste spam. Variant 1 is POST_TEXT verbatim; the others
# reword/reorder only. Every fact (the five tree types, $5 retail / $3 wholesale,
# delivery, number) must stay IDENTICAL across variants -- never add a claim here.
POST_TEXT_V2 = (
    "\U0001F331 Plant once, enjoy for years! \U0001F331\n"
    "\n"
    "Strong, healthy grafted fruit trees for sale, ready to plant. "
    "Grafted trees fruit faster and stay true to type.\n"
    "\n"
    "\U0001F96D Mango\n"
    "\U0001F348 Pawpaw\n"
    "\U0001F34A Orange\n"
    "\U0001F34A Naartjie\n"
    "\U0001F34B Lemon\n"
    "\n"
    "\U0001F4B5 Retail: $5 per tree\n"
    "\U0001F4E6 Wholesale: $3 per tree\n"
    "\U0001F69A Delivery available\n"
    "\n"
    "\U0001F4F2 Order now: +263785632943"
)

POST_TEXT_V3 = (
    "\U0001F333 *Start your own orchard* \U0001F333\n"
    "\n"
    "Grafted Mango \U0001F96D, Pawpaw \U0001F348, Orange \U0001F34A, "
    "Naartjie \U0001F34A and Lemon \U0001F34B trees - strong, healthy and "
    "ready to plant.\n"
    "\n"
    "\U0001F4B5 $5 per tree (retail)\n"
    "\U0001F4E6 $3 per tree (wholesale)\n"
    "\U0001F69A Delivery available\n"
    "\n"
    "Grafted trees fruit faster and stay true to type.\n"
    "\n"
    "\U0001F4F2 Order now: +263785632943"
)

POST_TEXTS = [POST_TEXT, POST_TEXT_V2, POST_TEXT_V3]

# Per-group ref-coded wa.me link to the ad's own contact number (+263785632943).
# That number is Genaie's (the bot's WhatsApp number), so replies reach the bot
# and the public-service-gate ad-ref recorder counts them.
TRACKING = {
    "prefix": "FT",
    "number": "+263785632943",
    "label": "fruit trees",
}


def main():
    return promo_broadcast.broadcast(
        text=POST_TEXTS,
        image=IMAGE_PATH,
        targets_file=TARGETS_FILE,
        state_file=STATE_FILE,
        send_delay=SEND_DELAY_SECONDS,
        tracking=TRACKING,
        promo_name="fruit-trees-promo",
    )


if __name__ == "__main__":
    sys.exit(main())
