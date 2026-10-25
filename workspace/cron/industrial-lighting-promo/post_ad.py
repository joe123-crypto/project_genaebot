#!/usr/bin/env python3
"""
Industrial Lighting solar daily ad poster.

Posts a FIXED, predefined advert (caption + image) for Industrial Lighting
(Pvt) Ltd's solar packages to a configured set of WhatsApp groups, once per day
per group. There is NO AI/LLM generation involved: the post text is a set of
hardcoded caption variants and the image is one bundled poster. This script is
meant to run from an OpenClaw `--command` cron (deterministic, no agent turn).

The operator runs this as a lead-gen ad on the second WhatsApp line (the "ads"
account): the contact number in the copy is the posting account's own via the
{bot_number} placeholder, so replies reach the bot and the leads are attributable
under the "IL" ad-ref prefix (in the public-service-gate's AD_REF_PREFIXES) --
the same lead-gen pattern as the big-player / skylight / genaie promos. The
advertiser-supplied poster already carries the packages, prices and the
advertiser's own branding, so the caption stays to one short line plus the
engine's click-to-chat link.

Delivery, invite/JID resolution, per-group state, caption rotation, the ref link
and the permission-error skip all live in the shared
`workspace/lib/promo_broadcast.py` engine (identical across every promo cron);
this script only supplies the advert text, image, and file paths.

Targets come from targets.json (JIDs and/or invite links). The bot must already
be a MEMBER of each group for sending to work.

Usage:
  post_ad.py            normal run (resolve, skip groups already sent today, send)
  post_ad.py --dry-run  show resolved targets, each group's caption variant + ref,
                        and every caption variant; no send, no state mutation
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
    "INDUSTRIAL_LIGHTING_PROMO_TARGETS", os.path.join(HERE, "targets.json")
)
STATE_FILE = os.environ.get(
    "INDUSTRIAL_LIGHTING_PROMO_STATE",
    "/home/joseph/.openclaw/runtime/cron-state/industrial-lighting-promo/state.json",
)

# One bundled ad poster (the advertiser-supplied image).
# INDUSTRIAL_LIGHTING_PROMO_IMAGE overrides it (a single explicit path) for testing.
IMAGE = os.environ.get("INDUSTRIAL_LIGHTING_PROMO_IMAGE") or os.path.join(
    HERE, "assets", "industrial-lighting-1.jpg"
)

# Fixed advert caption. The poster carries the packages, prices and advertiser
# branding, so the copy is one short line plus the contact number; the shared
# engine appends the ref-coded click-to-chat link. The contact number is the
# posting account's own, substituted into {bot_number} (so replies reach the bot,
# ref prefix "IL"). Emoji are written as explicit \u / \U escapes so the source
# stays ASCII-safe. {bot_number} is the only "fact" and is identical across all
# three variants (deploy/test_promo_captions_tracking.py pins that); the variants
# reword only the digit-free intro line.
_CONTACT = "\U0001F4DE WhatsApp {bot_number}"

POST_TEXT = (
    "\U00002600\U0000FE0F Reliable solar for homes, businesses and boreholes.\n"
    + _CONTACT
)

POST_TEXT_V2 = (
    "\U00002600\U0000FE0F Clean, dependable solar power for homes, businesses "
    "and boreholes.\n"
    + _CONTACT
)

POST_TEXT_V3 = (
    "\U00002600\U0000FE0F Solar systems for your home, business or borehole.\n"
    + _CONTACT
)

POST_TEXTS = [POST_TEXT, POST_TEXT_V2, POST_TEXT_V3]

# Per-group ref-coded wa.me link. The ad's contact number is the posting account's
# own ({bot_number} resolves to membership.json's defaults.accounts entry for the
# account that posts to each group), so replies reach the bot and the
# public-service-gate ad-ref recorder counts them (the "IL" prefix is in its
# AD_REF_PREFIXES list).
TRACKING = {
    "prefix": "IL",
    "label": "solar",
}


def main():
    return promo_broadcast.broadcast(
        text=POST_TEXTS,
        image=IMAGE,
        targets_file=TARGETS_FILE,
        state_file=STATE_FILE,
        tracking=TRACKING,
        promo_name="industrial-lighting-promo",
    )


if __name__ == "__main__":
    sys.exit(main())
