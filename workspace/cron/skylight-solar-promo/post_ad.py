#!/usr/bin/env python3
"""
Skylight-solar daily ad poster.

Posts a FIXED, predefined advert (caption + rotating image) for solar power
systems and complete water/borehole solutions to a configured set of WhatsApp
groups, once per day per group. There is NO AI/LLM generation involved: the post
text is a set of hardcoded caption variants and the images are two bundled
posters that rotate daily. This script is meant to run from an OpenClaw
`--command` cron (deterministic, no agent turn).

The two posters are the operator-supplied Skylight Solar Company adverts (one for
solar system packages, one for "Complete Water Solutions": survey, drilling,
flushing, installation). Their prices and package details live on the images
themselves; the captions deliberately carry no prices and drop the source brand
from the text (the logo stays baked into the image), routing replies to the bot's
own number -- the same lead-gen pattern as the borehole / fruit-trees / genaie
promos.

Delivery, invite/JID resolution, per-group state, and the permission-error skip
all live in the shared `workspace/lib/promo_broadcast.py` engine (identical across
the cv-promo, genaie-promo, building-materials-promo, fruit-trees-promo and
borehole-drilling-promo crons); this script only supplies the advert text,
images, and file paths.

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
    "SKYLIGHT_TARGETS", os.path.join(HERE, "targets.json")
)
STATE_FILE = os.environ.get(
    "SKYLIGHT_STATE",
    "/home/joseph/.openclaw/runtime/cron-state/skylight-solar-promo/state.json",
)

# Two bundled ad posters that rotate daily: the shared engine picks one by today's
# date ordinal, so a group never sees the same image on consecutive days.
# skylight-1 is the solar system packages poster, skylight-2 the water/borehole
# ("Complete Water Solutions") poster. SKYLIGHT_IMAGE overrides both (a single
# explicit path) for testing. Add more assets to lengthen the rotation.
_IMAGE_ASSETS = [
    os.path.join(HERE, "assets", "skylight-1.jpg"),
    os.path.join(HERE, "assets", "skylight-2.jpg"),
]
IMAGE = os.environ.get("SKYLIGHT_IMAGE") or _IMAGE_ASSETS

# Fixed advert. The posters carry the prices and package details; the captions
# stay price-free and brand-neutral and cover BOTH product lines (solar systems
# and water/borehole solutions). Emoji are kept as explicit \U escapes so the
# source stays ASCII-safe.
POST_TEXT = (
    "☀️ *Solar systems & complete water solutions* \U0001F4A7\n"
    "Power your home and bring water closer - professional installation you can "
    "depend on.\n"
    "\n"
    "⚡ *SOLAR SYSTEM PACKAGES* - inverter, solar panels, lithium batteries, "
    "mounting kit, protection kit & full installation. Run your TV, fridge, "
    "lights, fan & borehole uninterrupted. Free installation.\n"
    "\U0001F4A7 *COMPLETE WATER SOLUTIONS* - Survey • Drilling • "
    "Flushing • Installation.\n"
    "\n"
    "\U0001F4F7 See the packages & prices in the image.\n"
    "\U0001F4DE WhatsApp: {bot_number} - Genaie"
)

# Caption variants that rotate per group per day (see promo_broadcast) so the ad
# doesn't read as copy-paste spam. Variant 1 is POST_TEXT verbatim; the others
# reword/reorder only. Every fact (the two product lines, their contents, free
# installation, location of prices, number) must stay IDENTICAL across variants --
# never add a claim or a price here (the prices live on the image).
POST_TEXT_V2 = (
    "\U0001F4A1 *Go solar. Get water.* One team, two complete solutions.\n"
    "\n"
    "⚡ *Solar system packages* with inverter, solar panels, lithium "
    "batteries, mounting & protection kits and full installation - power your TV, "
    "fridge, lights, fan and borehole uninterrupted. Free installation.\n"
    "\U0001F4A7 *Complete water solutions* - Survey, Drilling, Flushing & "
    "Installation. Professional work you can depend on.\n"
    "\n"
    "\U0001F4F7 Packages & prices are in the image.\n"
    "\U0001F4DE WhatsApp: {bot_number} - Genaie"
)

POST_TEXT_V3 = (
    "\U0001F3E1 *Uninterrupted power and water for your home* ☀️"
    "\U0001F4A7\n"
    "\n"
    "✅ *SOLAR:* inverter, solar panels, lithium batteries, mounting & "
    "protection kits, all accessories & installation - runs TV, fridge, lights, "
    "fan and borehole. Free installation.\n"
    "✅ *WATER:* complete borehole solutions - Survey, Drilling, Flushing & "
    "Installation.\n"
    "\n"
    "Professional installation you can depend on.\n"
    "\U0001F4F7 See the packages & prices in the image.\n"
    "\U0001F4DE WhatsApp: {bot_number} - Genaie"
)

POST_TEXTS = [POST_TEXT, POST_TEXT_V2, POST_TEXT_V3]

# Per-group ref-coded wa.me link. The ad's contact number is the posting
# account's own ({bot_number} resolves to membership.json's defaults.accounts
# entry for the account that posts to each group), so replies reach the bot and
# the public-service-gate ad-ref recorder counts them (the "SK" prefix is in its
# AD_REF_PREFIXES list).
TRACKING = {
    "prefix": "SK",
    "label": "solar & borehole",
}


def main():
    return promo_broadcast.broadcast(
        text=POST_TEXTS,
        image=IMAGE,
        targets_file=TARGETS_FILE,
        state_file=STATE_FILE,
        tracking=TRACKING,
        promo_name="skylight-solar-promo",
    )


if __name__ == "__main__":
    sys.exit(main())
