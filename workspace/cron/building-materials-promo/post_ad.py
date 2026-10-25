#!/usr/bin/env python3
"""
Building-materials daily ad poster.

Posts a FIXED, predefined advert (caption + image) for a building-materials
for-sale business (quarry stones, pit sand, river sand) to a configured set of
WhatsApp groups, once per day per group. There is NO AI/LLM generation involved:
the post text is a hardcoded constant and the image is a bundled asset. This
script is meant to run from an OpenClaw `--command` cron (deterministic, no agent
turn).

Delivery, invite/JID resolution, per-group state, and the permission-error skip
all live in the shared `workspace/lib/promo_broadcast.py` engine (identical across
the cv-promo and genaie-promo crons); this script only supplies the advert text,
image, and file paths.

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
    "BUILDING_MATERIALS_TARGETS", os.path.join(HERE, "targets.json")
)
IMAGE_PATH = os.environ.get(
    "BUILDING_MATERIALS_IMAGE", os.path.join(HERE, "assets", "building-materials.jpg")
)
STATE_FILE = os.environ.get(
    "BUILDING_MATERIALS_STATE",
    "/home/joseph/.openclaw/runtime/cron-state/building-materials-promo/state.json",
)

# Fixed advert. Verbatim marketing copy supplied by the operator. Emoji are kept
# as explicit \U escapes so the source stays ASCII-safe.
POST_TEXT = (
    "BUILDING MATERIALS FOR SALE! \U0001F3D7️\n"
    "\n"
    "\U0001F9F1 Bricks:\n"
    "  - Red commons: $95 per 1000\n"
    "  - Blue heart: $100 per 1000\n"
    "\n"
    "\U0001FAA8 Quarry Stones: $25 per cubic\n"
    "\U0001F3DE️ River Sand: $350 per 15 cubic\n"
    "\U0001F3D6️ Pit Sand: $340 per 15 cubic\n"
    "\U000026F0️ Gravel: $220 per 15 cubic\n"
    "\n"
    "\U0001F4E6 Available in bulk quantities\n"
    "\n"
    "\U0001F4DE Contact us:\n"
    "\n"
    "\U0001F4AC WhatsApp & Calls: +263 775 489 417\n"
    "\U0001F4DE Phone Calls Only: {bot_number}\n"
    "\n"
    "\U0001F4E9 Contact us now to place your order!"
)

# Caption variants that rotate per group per day (see promo_broadcast) so the ad
# doesn't read as copy-paste spam. Variant 1 is POST_TEXT verbatim; the others
# reword/reorder only. Every fact (materials, prices, units, both numbers and
# which line takes WhatsApp vs calls only) must stay IDENTICAL across variants --
# never add a claim or offer here.
POST_TEXT_V2 = (
    "\U0001F3E0 Building or renovating? Get your materials here! \U0001F477\n"
    "\n"
    "\U0001FAA8 Quarry Stones: $25 per cubic\n"
    "\U000026F0️ Gravel: $220 per 15 cubic\n"
    "\U0001F3DE️ River Sand: $350 per 15 cubic\n"
    "\U0001F3D6️ Pit Sand: $340 per 15 cubic\n"
    "\n"
    "\U0001F9F1 Bricks:\n"
    "  - Red commons: $95 per 1000\n"
    "  - Blue heart: $100 per 1000\n"
    "\n"
    "\U0001F4E6 Bulk quantities available\n"
    "\n"
    "\U0001F4AC WhatsApp & Calls: +263 775 489 417\n"
    "\U0001F4DE Phone Calls Only: {bot_number}\n"
    "\n"
    "Place your order today! \U0001F4E9"
)

POST_TEXT_V3 = (
    "\U0001F9F1 *Bricks, sand, gravel & quarry stones for sale* \U0001F3D7️\n"
    "\n"
    "Bricks (per 1000):\n"
    "  • Red commons - $95\n"
    "  • Blue heart - $100\n"
    "\n"
    "Per 15 cubic:\n"
    "  • River Sand - $350\n"
    "  • Pit Sand - $340\n"
    "  • Gravel - $220\n"
    "\n"
    "Per cubic:\n"
    "  • Quarry Stones - $25\n"
    "\n"
    "\U0001F4E6 Available in bulk quantities\n"
    "\n"
    "To order:\n"
    "\U0001F4AC WhatsApp & Calls: +263 775 489 417\n"
    "\U0001F4DE Phone Calls Only: {bot_number}"
)

POST_TEXTS = [POST_TEXT, POST_TEXT_V2, POST_TEXT_V3]

# Per-group ref-coded wa.me link to the configured contact account.
# {bot_number} and the link use defaults.contact_account when selected;
# otherwise they use the posting account. Replies reach the bot's ad-ref recorder.
TRACKING = {
    "prefix": "BM",
    "label": "building materials",
}


def main():
    return promo_broadcast.broadcast(
        text=POST_TEXTS,
        image=IMAGE_PATH,
        targets_file=TARGETS_FILE,
        state_file=STATE_FILE,
        tracking=TRACKING,
        promo_name="building-materials-promo",
    )


if __name__ == "__main__":
    sys.exit(main())
