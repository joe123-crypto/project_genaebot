#!/usr/bin/env python3
"""
Borehole-paid daily ad poster.

Posts a FIXED, predefined PAID ADVERT (caption + image) for a third-party
borehole siting/drilling company to a configured set of WhatsApp groups, once per
day per group. There is NO AI/LLM generation involved: the post text is a set of
hardcoded caption variants and the image is one bundled poster. This script is
meant to run from an OpenClaw `--command` cron (deterministic, no agent turn).

This is a PAID ADVERT the operator runs on behalf of an advertiser, so it is kept
separate from the operator's own `borehole-drilling-promo`. The advertiser's brand,
taglines and catalog link are deliberately stripped from the caption and every
contact number is replaced with the bot's own line: replies reach the bot (the
"PB" prefix is in the public-service-gate's AD_REF_PREFIXES) so the leads this paid
ad brings in are attributable -- the same lead-gen pattern as the skylight /
borehole-drilling / genaie promos.

Delivery, invite/JID resolution, per-group state, caption rotation, the ref link
and the permission-error skip all live in the shared
`workspace/lib/promo_broadcast.py` engine (identical across every promo cron); this
script only supplies the advert text, image, and file paths.

Targets come from targets.json (JIDs and/or invite links). The bot must already be
a MEMBER of each group for sending to work.

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
    "BOREHOLE_PAID_TARGETS", os.path.join(HERE, "targets.json")
)
STATE_FILE = os.environ.get(
    "BOREHOLE_PAID_STATE",
    "/home/joseph/.openclaw/runtime/cron-state/borehole-paid-promo/state.json",
)

# One bundled ad poster (the advertiser-supplied image). BOREHOLE_PAID_IMAGE
# overrides it (a single explicit path) for testing.
IMAGE = os.environ.get("BOREHOLE_PAID_IMAGE") or os.path.join(
    HERE, "assets", "borehole-paid-1.jpg"
)

# Fixed PAID ADVERT caption. All branding (company name, taglines, catalog link)
# is stripped and every contact number is the configured contact line, substituted
# into {bot_number}. Emoji are kept as explicit \u / \U escapes so the source
# stays ASCII-safe (❗ = red exclamation mark). The caption carries NO digits
# other than the {bot_number} placeholder, so every variant shares identical
# "facts" (deploy/test_promo_captions_tracking.py pins that).
_AREAS = (
    "*Mashonaland Central:* Harare, Chegutu, Kwekwe, Gokwe, Gweru, Zvishavane, "
    "Bindura, Mvurwi, Guruve\n"
    "*Mashonaland West:* Banket, Chinhoyi, Karoi, Kariba\n"
    "*Mashonaland East:* Marondera, Macheke, Headlands, Rusape\n"
    "*Manicaland:* Mutare, Nyanga, Chipinge, Birchenough Bridge, Buhera, "
    "Murambinda\n"
    "*Masvingo:* Masvingo City, Gutu, Bikita, Chiredzi, Chivi\n"
    "*Matabeleland:* Bulawayo, Plumtree, Gwanda, Lupani, Tsholotsho, Beitbridge\n"
)
_SERVICES = (
    "*Other services:*\n"
    "• Borehole survey / siting\n"
    "• Borehole drilling & casing\n"
    "• Pump fishing out\n"
    "• Borehole flushing\n"
    "• Capacity testing\n"
    "• Deepening\n"
    "• Borehole repairing\n"
    "• Pump installation (solar & electric)\n"
)
_CONTACT = (
    "\U0001F4DE Call / WhatsApp: {bot_number}\n"
    "\U0001F6D1 Payment is made on site"
)
_HEADER = "❗❗❗ *PAID ADVERT* ❗❗❗\n\n"

POST_TEXT = (
    _HEADER
    + "\U0001F4A7 *Borehole Siting & Drilling Services*\n"
    "\n"
    "Good day ladies and gentlemen. We carry out borehole siting and drilling in "
    "the following areas:\n"
    "\n"
    + _AREAS
    + "\n"
    "...and surrounding areas. If you have a hole in any of the areas above, "
    "contact us for favourable rates and quotes.\n"
    "\n"
    + _SERVICES
    + "\n"
    + _CONTACT
)

# Variants reword only the intro and closing sentence; the area list, services
# list and {bot_number} stay identical so the facts never drift between variants.
POST_TEXT_V2 = (
    _HEADER
    + "\U0001F4A7 *Borehole Siting & Drilling Services*\n"
    "\n"
    "Reliable water for your home or business starts with the right borehole. We "
    "offer professional siting and drilling across the following areas:\n"
    "\n"
    + _AREAS
    + "\n"
    "...and surrounding areas. Got a hole in any of the areas above? Get in touch "
    "for favourable rates and a quote.\n"
    "\n"
    + _SERVICES
    + "\n"
    + _CONTACT
)

POST_TEXT_V3 = (
    _HEADER
    + "\U0001F4A7 *Borehole Siting & Drilling Services*\n"
    "\n"
    "Professional borehole siting and drilling you can depend on. We cover the "
    "following areas:\n"
    "\n"
    + _AREAS
    + "\n"
    "...and surrounding areas. If your area is listed above, reach out for "
    "favourable rates and quotes.\n"
    "\n"
    + _SERVICES
    + "\n"
    + _CONTACT
)

POST_TEXTS = [POST_TEXT, POST_TEXT_V2, POST_TEXT_V3]

# Per-group ref-coded wa.me link to the configured contact account.
# {bot_number} and the link use defaults.contact_account when selected;
# otherwise they use the posting account. Replies reach the bot's ad-ref recorder.
TRACKING = {
    "prefix": "PB",
    "label": "borehole",
}


def main():
    return promo_broadcast.broadcast(
        text=POST_TEXTS,
        image=IMAGE,
        targets_file=TARGETS_FILE,
        state_file=STATE_FILE,
        tracking=TRACKING,
        promo_name="borehole-paid-promo",
    )


if __name__ == "__main__":
    sys.exit(main())
