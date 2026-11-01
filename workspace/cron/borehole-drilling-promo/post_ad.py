#!/usr/bin/env python3
"""
Borehole-drilling daily ad poster.

Posts a general drilling and solar-installation advert with four photos rotating
daily. Provider-specific prices and packages are supplied by the customer-service
catalog rather than the advert. Deterministic; no agent turn.

Delivery, invite/JID resolution, per-group state, the weekly cadence, and the
permission-error skip all live in the shared `workspace/lib/promo_broadcast.py`
engine (identical across the cv-promo, genaie-promo, building-materials-promo and
fruit-trees-promo crons); this script only supplies the advert text, images, and
file paths.

Targets come from targets.json (JIDs and/or invite links). Invite links are
resolved to group JIDs via the OpenClaw WhatsApp directory; the bot must already
be a MEMBER of each group for resolution and sending to work. Resolved JIDs are
cached in the state file to avoid re-resolving every day. Groups under the file's
`weekly_groups` map are posted to only on their configured weekdays (MUTANDIRI
VILLAGE RESIDENTS gets the ad three times a week: Mon/Wed/Fri).

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
    "BOREHOLE_TARGETS", os.path.join(HERE, "targets.json")
)
STATE_FILE = os.environ.get(
    "BOREHOLE_STATE",
    "/home/joseph/.openclaw/runtime/cron-state/borehole-drilling-promo/state.json",
)

# Four bundled ad photos that rotate daily: the shared engine picks one by today's
# date ordinal, so a group never sees the same image on consecutive days.
# BOREHOLE_IMAGE overrides all of them (a single explicit path) for testing.
# Add more assets to lengthen the rotation.
_IMAGE_ASSETS = [
    os.path.join(HERE, "assets", "borehole-1.jpg"),
    os.path.join(HERE, "assets", "borehole-2.jpg"),
    os.path.join(HERE, "assets", "borehole-3.jpg"),
    os.path.join(HERE, "assets", "borehole-4.jpg"),
]
IMAGE = os.environ.get("BOREHOLE_IMAGE") or _IMAGE_ASSETS

# General captions; provider-specific packages and prices live in data/borehole-providers.json.
POST_TEXT = (
    "💧 *Bring water closer to home.* 💧\n"
    "*Professional borehole drilling & solar installation.*\n\n"
    "Need a borehole drilled or a solar pumping system installed? Get in touch "
    "to discuss the options available for your area.\n\n"
    "📍 Services across Zimbabwe.\n"
    "💬 Inbox us with your town or area and what you need. We’ll help you find "
    "a suitable offer and explain the packages and prices.\n\n"
    "📞 WhatsApp: {bot_number} - Genaie"
)
POST_TEXT_V2 = (
    "☀️ *Water for your home, farm or business.* 💧\n\n"
    "Looking for borehole drilling or solar borehole installation? Ask us about "
    "the available services, installation packages and prices.\n\n"
    "📍 Services across Zimbabwe.\n"
    "💬 Where are you located, and what do you need? Inbox us so we can help "
    "you find a suitable option for your area.\n\n"
    "📞 WhatsApp: {bot_number} - Genaie"
)
POST_TEXT_V3 = (
    "🚰 *Planning a borehole or solar water system?*\n\n"
    "We can help you explore borehole drilling and solar installation options "
    "for your home, farm or business.\n\n"
    "💬 Send us your town or area and your requirements. We’ll explain the "
    "suitable offers, what’s included and the prices.\n\n"
    "📍 Services across Zimbabwe.\n"
    "📞 WhatsApp: {bot_number} - Genaie"
)
POST_TEXTS = [POST_TEXT, POST_TEXT_V2, POST_TEXT_V3]

# Per-group ref-coded wa.me link to the configured contact account.
# {bot_number} and the link use defaults.contact_account when selected;
# otherwise they use the posting account. Replies reach the bot's ad-ref recorder.
TRACKING = {
    "prefix": "BH",
    "label": "borehole",
}


def main():
    return promo_broadcast.broadcast(
        text=POST_TEXTS,
        image=IMAGE,
        targets_file=TARGETS_FILE,
        state_file=STATE_FILE,
        tracking=TRACKING,
        promo_name="borehole-drilling-promo",
    )


if __name__ == "__main__":
    sys.exit(main())
