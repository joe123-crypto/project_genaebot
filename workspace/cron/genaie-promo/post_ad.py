#!/usr/bin/env python3
"""
Genaie-services daily ad poster.

Posts a FIXED, predefined advert (caption + image) for Genaie's own services
(lead generation and WhatsApp group advertising) to a configured set of
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
    "GENAIE_PROMO_TARGETS", os.path.join(HERE, "targets.json")
)
IMAGE_PATH = os.environ.get(
    "GENAIE_PROMO_IMAGE", os.path.join(HERE, "assets", "genaie-services.png")
)
STATE_FILE = os.environ.get(
    "GENAIE_PROMO_STATE",
    "/home/joseph/.openclaw/runtime/cron-state/genaie-promo/state.json",
)

# Fixed advert. Verbatim marketing copy supplied by the operator. Emoji are kept
# as explicit \U escapes so the source stays ASCII-safe.
POST_TEXT = (
    "\U0001F4E2 Get Your Business Noticed\n"
    "Get Customers Ready To Buy\n"
    "\n"
    "Great products go unnoticed when nobody knows they exist. "
    "Genaie fixes that.\n"
    "\n"
    "\U0001F9F2 Lead Generation\n"
    "\U0001F7E2 WhatsApp Group Ads\n"
    "\n"
    "\U0001F4DE WhatsApp us: {bot_number}"
)

# Caption variants that rotate per group per day (see promo_broadcast) so the ad
# doesn't read as copy-paste spam. Variant 1 is POST_TEXT verbatim; the others
# reword/reorder only. Every fact (the two services, number) must stay
# IDENTICAL across variants -- never add a claim or offer here.
POST_TEXT_V2 = (
    "\U0001F440 Do people know your business exists?\n"
    "\n"
    "Great products go unnoticed when nobody knows they exist. "
    "Genaie fixes that.\n"
    "\n"
    "\U0001F7E2 WhatsApp Group Ads\n"
    "\U0001F9F2 Lead Generation\n"
    "\n"
    "Get your business noticed and get customers ready to buy.\n"
    "\n"
    "\U0001F4DE WhatsApp us: {bot_number}"
)

POST_TEXT_V3 = (
    "\U0001F4E2 *Great products deserve to be noticed*\n"
    "\n"
    "✔️ Get your business noticed\n"
    "✔️ Get customers ready to buy\n"
    "\n"
    "Our services:\n"
    "\U0001F9F2 Lead Generation\n"
    "\U0001F7E2 WhatsApp Group Ads\n"
    "\n"
    "\U0001F4DE WhatsApp us: {bot_number}"
)

POST_TEXTS = [POST_TEXT, POST_TEXT_V2, POST_TEXT_V3]

# Per-group ref-coded wa.me link to the configured contact account.
# {bot_number} and the link use defaults.contact_account when selected;
# otherwise they use the posting account. Replies reach the bot's ad-ref recorder.
TRACKING = {
    "prefix": "GN",
    "label": "Genaie",
}


def main():
    return promo_broadcast.broadcast(
        text=POST_TEXTS,
        image=IMAGE_PATH,
        targets_file=TARGETS_FILE,
        state_file=STATE_FILE,
        tracking=TRACKING,
        promo_name="genaie-promo",
    )


if __name__ == "__main__":
    sys.exit(main())
