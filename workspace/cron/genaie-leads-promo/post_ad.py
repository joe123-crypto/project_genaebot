#!/usr/bin/env python3
"""
Genaie lead-generation daily ad poster.

Posts a FIXED, predefined advert (caption + image) for Genaie's lead-generation
offer ("we generate potential customers; you choose how you pay") to a configured
set of WhatsApp groups, once per day per group. This is a SEPARATE campaign from
the genaie-promo services ad: it runs later in the day (17:00) with its own images
and pricing-focused copy. There is NO AI/LLM generation involved: the post text is
a hardcoded constant and the images are bundled assets that alternate daily. This
script is meant to run from an OpenClaw `--command` cron (deterministic, no agent
turn).

Delivery, invite/JID resolution, per-group state, and the permission-error skip
all live in the shared `workspace/lib/promo_broadcast.py` engine (identical across
the genaie-promo, cv-promo and building-materials-promo crons); this script only
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
    "GENAIE_LEADS_PROMO_TARGETS", os.path.join(HERE, "targets.json")
)
# Two bundled ad images that alternate daily: the shared engine picks one by
# today's date ordinal, so a group never sees the same image two days running.
# GENAIE_LEADS_PROMO_IMAGE overrides both (a single explicit path) for testing.
# Add more assets to lengthen the rotation.
_IMAGE_ASSETS = [
    os.path.join(HERE, "assets", "genaie-leads.png"),
    os.path.join(HERE, "assets", "genaie-leads-2.png"),
]
IMAGE = os.environ.get("GENAIE_LEADS_PROMO_IMAGE") or _IMAGE_ASSETS
STATE_FILE = os.environ.get(
    "GENAIE_LEADS_PROMO_STATE",
    "/home/joseph/.openclaw/runtime/cron-state/genaie-leads-promo/state.json",
)

# Fixed advert. Verbatim marketing copy supplied by the operator. Emoji are kept
# as explicit \U escapes so the source stays ASCII-safe.
POST_TEXT = (
    "\U0001F4E2 We Generate Potential Customers For Your Business\n"
    "\n"
    "You choose the model that suits your business:\n"
    "\n"
    "\u2705 Pay per lead\n"
    "\u2705 Fixed monthly payment\n"
    "\u2705 Pay a % of revenue\n"
    "\n"
    "\U0001F4DE WhatsApp us: +263785632943"
)

# Caption variants that rotate per group per day (see promo_broadcast) so the ad
# doesn't read as copy-paste spam. Variant 1 is POST_TEXT verbatim; the others
# reword/reorder only. Every fact (the three payment models, number) must stay
# IDENTICAL across variants -- never add a claim or offer here.
POST_TEXT_V2 = (
    "\U0001F4C8 Need more customers for your business?\n"
    "\n"
    "We generate potential customers for you - and you pick how you pay:\n"
    "\n"
    "✅ Pay per lead\n"
    "✅ Fixed monthly payment\n"
    "✅ Pay a % of revenue\n"
    "\n"
    "\U0001F4DE WhatsApp us: +263785632943"
)

POST_TEXT_V3 = (
    "\U0001F91D *Potential customers for your business, your way*\n"
    "\n"
    "✅ Fixed monthly payment\n"
    "✅ Pay per lead\n"
    "✅ Pay a % of revenue\n"
    "\n"
    "Choose the model that suits your business.\n"
    "\n"
    "\U0001F4DE WhatsApp us: +263785632943"
)

POST_TEXTS = [POST_TEXT, POST_TEXT_V2, POST_TEXT_V3]

# Per-group ref-coded wa.me link to Genaie's own number (+263785632943, the
# bot's WhatsApp number), so replies reach the bot and the public-service-gate
# ad-ref recorder counts them.
TRACKING = {
    "prefix": "GL",
    "number": "+263785632943",
    "label": "lead generation",
}


def main():
    return promo_broadcast.broadcast(
        text=POST_TEXTS,
        image=IMAGE,
        targets_file=TARGETS_FILE,
        state_file=STATE_FILE,
        tracking=TRACKING,
        promo_name="genaie-leads-promo",
    )


if __name__ == "__main__":
    sys.exit(main())
