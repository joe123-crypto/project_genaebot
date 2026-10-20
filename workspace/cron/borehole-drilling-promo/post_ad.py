#!/usr/bin/env python3
"""
Borehole-drilling daily ad poster.

Posts a FIXED, predefined advert (caption + rotating image) for solar borehole
system installation packages (0.75HP / 1HP / 1.5HP / 2HP DC pump packages) to a
configured set of WhatsApp groups, once per day per group. There is NO AI/LLM
generation involved: the post text is a set of hardcoded caption variants and the
images are bundled assets that rotate daily, except the El Nino variant, which
always uses assets/elnino.jpg and links the MSD seasonal forecast. This script is meant to run from an OpenClaw
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

# The El Nino caption variant (POST_TEXT_V4) always goes out with its own
# matching photo instead of the daily rotation above.
ELNINO_IMAGE = os.path.join(HERE, "assets", "elnino.jpg")

# Zimbabwe Meteorological Services Department 2026-2027 seasonal forecast, cited
# and linked by the El Nino variant.
FORECAST_URL = (
    "https://www.weatherzw.org.zw/products/seasonal-forecast/"
    "2026-2027-seasonal-forecast/"
)
# External source links a variant may carry on top of the shared ad facts.
SOURCE_URLS = (FORECAST_URL,)

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
    "\U0001F4DE WhatsApp: {bot_number} - Genaie"
)

# Caption variants that rotate per group per day (see promo_broadcast) so the ad
# doesn't read as copy-paste spam. Variant 1 is POST_TEXT verbatim; the others
# reword/reorder only. Every fact (packages, contents, prices, location, number)
# must stay IDENTICAL across variants -- never add a claim or offer here.
POST_TEXT_V2 = (
    "☀️ *Solar borehole systems, installed for you* ☀️\n"
    "Bring water closer to home with professional borehole drilling you can "
    "depend on.\n"
    "\n"
    "✅ *Every package includes:*\n"
    "  • 5000L Tank\n"
    "  • 4 Meters Tank Stand\n"
    "  • Pump & External Controller\n"
    "  • Solar Panels\n"
    "  • Solar Stand\n"
    "  • Pipes & Cables\n"
    "  • 1 Garden Tap\n"
    "  • All Accessories & Installation\n"
    "\n"
    "\U0001F4A7 *Choose your DC pump (with Control Box):*\n"
    "1️⃣ 0.75HP DC Pump - $1800\n"
    "2️⃣ 1HP DC Pump - $2000\n"
    "3️⃣ 1.5HP DC Pump - $2200\n"
    "4️⃣ 2HP DC Pump - $2400\n"
    "\n"
    "\U0001F4CD Based in Bindura - We Come To You!\n"
    "\U0001F4DE WhatsApp: {bot_number} - Genaie"
)

POST_TEXT_V3 = (
    "\U0001F6B0 *Want water closer to home?*\n"
    "Complete solar borehole system installation packages. "
    "Based in Bindura - We Come To You! \U0001F4CD\n"
    "\n"
    "*BOREHOLE SYSTEM INSTALLATION PACKAGES*\n"
    "\U0001F4B5 Package 1 - 0.75HP DC Pump with Control Box: *$1800*\n"
    "\U0001F4B5 Package 2 - 1HP DC Pump with Control Box: *$2000*\n"
    "\U0001F4B5 Package 3 - 1.5HP DC Pump with Control Box: *$2200*\n"
    "\U0001F4B5 Package 4 - 2HP DC Pump with Control Box: *$2400*\n"
    "\n"
    "\U0001F4E6 All 4 packages come with: 5000L Tank • 4 Meters Tank Stand • "
    "Pump & External Controller • Solar Panels • Solar Stand • Pipes & Cables • "
    "1 Garden Tap • All Accessories & Installation\n"
    "\n"
    "\U0001F4A7 Professional borehole drilling you can depend on.\n"
    "\U0001F4DE WhatsApp: {bot_number} - Genaie"
)

# El Nino / dry-season variant. The ONLY variant allowed an external claim: the
# MSD seasonal forecast, attributed and linked via FORECAST_URL. Ad facts stay
# identical to the other variants.
POST_TEXT_V4 = (
    "\u2600\ufe0f *El Nino is coming. Are you prepared?* \u2600\ufe0f\n"
    "The Meteorological Services Department's seasonal forecast warns that El Nino "
    "could bring a drier-than-normal season, with below-normal rains and long dry "
    "spells expected, especially early in the season.\n"
    "\U0001F4C4 Read the forecast: " + FORECAST_URL + "\n"
    "\n"
    "Don't wait for the wells and taps to run dry. Get a solar borehole system "
    "installed now: professional borehole drilling you can depend on.\n"
    "\n"
    "*BOREHOLE SYSTEM INSTALLATION PACKAGES*\n"
    "\U0001F4A7 Package 1 - 0.75HP DC Pump with Control Box: *$1800*\n"
    "\U0001F4A7 Package 2 - 1HP DC Pump with Control Box: *$2000*\n"
    "\U0001F4A7 Package 3 - 1.5HP DC Pump with Control Box: *$2200*\n"
    "\U0001F4A7 Package 4 - 2HP DC Pump with Control Box: *$2400*\n"
    "\n"
    "\u2705 Every package includes: 5000L Tank \u2022 4 Meters Tank Stand \u2022 "
    "Pump & External Controller \u2022 Solar Panels \u2022 Solar Stand \u2022 "
    "Pipes & Cables \u2022 1 Garden Tap \u2022 All Accessories & Installation\n"
    "\n"
    "\U0001F4CD Based in Bindura - We Come To You!\n"
    "\U0001F4DE WhatsApp: {bot_number} - Genaie"
)

POST_TEXTS = [POST_TEXT, POST_TEXT_V2, POST_TEXT_V3, POST_TEXT_V4]

# Variant index -> fixed image. BOREHOLE_IMAGE (a test override) still forces a
# single image for every variant.
VARIANT_IMAGES = (
    {} if os.environ.get("BOREHOLE_IMAGE")
    else {POST_TEXTS.index(POST_TEXT_V4): ELNINO_IMAGE}
)

# Per-group ref-coded wa.me link. The ad's contact number is the posting
# account's own ({bot_number} resolves to membership.json's defaults.accounts
# entry for the account that posts to each group), so replies reach the bot and
# the public-service-gate ad-ref recorder counts them.
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
        variant_images=VARIANT_IMAGES,
    )


if __name__ == "__main__":
    sys.exit(main())
