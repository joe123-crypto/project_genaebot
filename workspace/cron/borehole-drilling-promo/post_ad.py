#!/usr/bin/env python3
"""
Borehole-drilling daily ad poster.

Posts a FIXED, predefined advert (caption + rotating image) for solar borehole
system installation packages (0.75HP / 1HP / 1.5HP / 2HP DC pump packages) to a
configured set of WhatsApp groups, once per day per group. There is NO AI/LLM
generation involved: the post text is a hardcoded constant and the images are
bundled assets that rotate daily. This script is meant to run from an OpenClaw
`--command` cron (deterministic, no agent turn).

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
    "BOREHOLE_TARGETS", os.path.join(HERE, "targets.json")
)
STATE_FILE = os.environ.get(
    "BOREHOLE_STATE",
    "/home/joseph/.openclaw/runtime/cron-state/borehole-drilling-promo/state.json",
)
SEND_DELAY_SECONDS = float(os.environ.get("BOREHOLE_SEND_DELAY", "3"))

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

# Fixed advert. Package details are extracted from the operator-supplied poster
# (packages only; the source brand/logo is deliberately dropped). Emoji are kept
# as explicit \U escapes so the source stays ASCII-safe.
POST_TEXT = (
    "\U0001F4A7 *Bring water closer to home.* \U0001F4A7\n"
    "*Professional borehole drilling you can depend on.*\n"
    "\n"
    "*BOREHOLE SYSTEM INSTALLATION PACKAGES*\n"
    "\n"
    "\U0001F4E6 PACKAGE 1: 0.75HP DC Pump with Control Box\n"
    "  • 5000L Tank\n"
    "  • 4 Meters Tank Stand\n"
    "  • 0.75HP DC Pump\n"
    "  • Pump & External Controller\n"
    "  • Solar Panels\n"
    "  • Solar Stand\n"
    "  • Pipes & Cables\n"
    "  • 1 Garden Tap\n"
    "  • All Accessories & Installation\n"
    "\U0001F4B5 Price: $1800\n"
    "\n"
    "\U0001F4E6 PACKAGE 2: 1HP DC Pump with Control Box\n"
    "  • 5000L Tank\n"
    "  • 4 Meters Tank Stand\n"
    "  • 1HP DC Pump\n"
    "  • Pump & External Controller\n"
    "  • Solar Panels\n"
    "  • Solar Stand\n"
    "  • Pipes & Cables\n"
    "  • 1 Garden Tap\n"
    "  • All Accessories & Installation\n"
    "\U0001F4B5 Price: $2000\n"
    "\n"
    "\U0001F4E6 PACKAGE 3: 1.5HP DC Pump with Control Box\n"
    "  • 5000L Tank\n"
    "  • 4 Meters Tank Stand\n"
    "  • 1.5HP DC Pump\n"
    "  • Pump & External Controller\n"
    "  • Solar Panels\n"
    "  • Solar Stand\n"
    "  • Pipes & Cables\n"
    "  • 1 Garden Tap\n"
    "  • All Accessories & Installation\n"
    "\U0001F4B5 Price: $2200\n"
    "\n"
    "\U0001F4E6 PACKAGE 4: 2HP DC Pump with Control Box\n"
    "  • 5000L Tank\n"
    "  • 4 Meters Tank Stand\n"
    "  • 2HP DC Pump\n"
    "  • Pump & External Controller\n"
    "  • Solar Panels\n"
    "  • Solar Stand\n"
    "  • Pipes & Cables\n"
    "  • 1 Garden Tap\n"
    "  • All Accessories & Installation\n"
    "\U0001F4B5 Price: $2400\n"
    "\n"
    "\U0001F4CD Based in Bindura - We Come To You!\n"
    "\U0001F4DE WhatsApp: +263785632943 - Genaie"
)


def main():
    return promo_broadcast.broadcast(
        text=POST_TEXT,
        image=IMAGE,
        targets_file=TARGETS_FILE,
        state_file=STATE_FILE,
        send_delay=SEND_DELAY_SECONDS,
    )


if __name__ == "__main__":
    sys.exit(main())
