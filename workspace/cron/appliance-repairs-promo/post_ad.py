#!/usr/bin/env python3
"""
Appliance-repairs daily ad poster.

Posts a FIXED, predefined advert (caption + image) for APPLIANCE REPAIRS PL (a
Harare appliance repair shop: TVs, inverters, batteries, welding machines,
borehole drives, microwaves, PA systems) to a configured set of WhatsApp groups,
once per day per group. There is NO AI/LLM generation involved: the post text is
a hardcoded constant and the image is a bundled asset. This script is meant to
run from an OpenClaw `--command` cron (deterministic, no agent turn).

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
    "APPLIANCE_REPAIRS_TARGETS", os.path.join(HERE, "targets.json")
)
IMAGE_PATH = os.environ.get(
    "APPLIANCE_REPAIRS_IMAGE",
    os.path.join(HERE, "assets", "appliance-repairs-ad.jpg"),
)
STATE_FILE = os.environ.get(
    "APPLIANCE_REPAIRS_STATE",
    "/home/joseph/.openclaw/runtime/cron-state/appliance-repairs-promo/state.json",
)

# Fixed advert. Marketing copy supplied by the operator, verbatim except that its
# "Follow our page" line (which carried a WhatsApp channel link) was dropped at
# the operator's request: the only link in the ad is the tracking link appended
# by TRACKING below. Emoji are kept as explicit \U escapes so the source stays
# ASCII-safe.
POST_TEXT = (
    "\U0001F603 HELLO ZIMBABWE \U0001F1FF\U0001F1FC\n"
    "\n"
    "Is your TV, Inverter, Battery, Welding Machine, Borehole Drive, Microwave, "
    "PA System, or any home appliance giving you problems?\n"
    "\n"
    "❌ Don't buy new before talking to the experts!\n"
    "\n"
    "At *APPLIANCE REPAIRS PL*, we bring dead appliances back to life - "
    "Professional, Reliable & Affordable!\n"
    "\n"
    "*WE REPAIR:*\n"
    "✅ TV Repairs - LED, LCD, Smart, Android & Projectors\n"
    "✅ PA Systems & Amplifiers\n"
    "✅ Washing Machines\n"
    "✅ Microwaves\n"
    "✅ Inverters - MUST, Kodak, Sako (Error 03/09, No Output)\n"
    "✅ Batteries & Solar Systems\n"
    "✅ Welding Machines & Borehole Drives\n"
    "✅ Electric Cookers, Ovens & All Home/Commercial Appliances\n"
    "\n"
    "*WHY CHOOSE US?*\n"
    "\U0001F697 We come to you - Home & Business calls\n"
    "\U0001F9FE Official Receipt\n"
    "\U0001F6E1️ 3-Month Warranty on all repairs\n"
    "\U0001F4B0 Save 70% - Repair vs Replace\n"
    "\n"
    "\U0001F4CD Onnon Building, 88 Corner Kaguvi st & Albion St, Harare CBD\n"
    "\U0001F4DE Call / WhatsApp:\n"
    "077 521 6711 | 071 391 1390 | 073 338 6791\n"
    "\n"
    "*APPLIANCE REPAIRS PL – Bringing Dead Appliances Back to Life! "
    "\U0001F527✨*"
)

# Caption variants that rotate per group per day (see promo_broadcast) so the ad
# doesn't read as copy-paste spam. Variant 1 is POST_TEXT verbatim; the others
# reword/reorder only. Every fact (the repair list, Error 03/09, home & business
# calls, receipt, 3-month warranty, save 70%, address, the three numbers) must
# stay IDENTICAL across variants -- never add a claim or a link here.
POST_TEXT_V2 = (
    "\U0001F527 *APPLIANCE REPAIRS PL* - we bring dead appliances back to life! "
    "✨\n"
    "\n"
    "❌ Don't buy new before talking to the experts. "
    "\U0001F4B0 Save 70% - Repair vs Replace\n"
    "\n"
    "*WE REPAIR:*\n"
    "✅ TV Repairs - LED, LCD, Smart, Android & Projectors\n"
    "✅ Inverters - MUST, Kodak, Sako (Error 03/09, No Output)\n"
    "✅ Batteries & Solar Systems\n"
    "✅ Welding Machines & Borehole Drives\n"
    "✅ PA Systems & Amplifiers\n"
    "✅ Washing Machines\n"
    "✅ Microwaves\n"
    "✅ Electric Cookers, Ovens & All Home/Commercial Appliances\n"
    "\n"
    "\U0001F697 We come to you - Home & Business calls\n"
    "\U0001F9FE Official Receipt\n"
    "\U0001F6E1️ 3-Month Warranty on all repairs\n"
    "\n"
    "\U0001F4CD Onnon Building, 88 Corner Kaguvi st & Albion St, Harare CBD\n"
    "\U0001F4DE Call / WhatsApp:\n"
    "077 521 6711 | 071 391 1390 | 073 338 6791"
)

POST_TEXT_V3 = (
    "\U0001F1FF\U0001F1FC *Appliance giving you problems?* Repair it, "
    "don't replace it!\n"
    "\n"
    "TV, Inverter, Battery, Welding Machine, Borehole Drive, Microwave, "
    "PA System or any home appliance - talk to the experts at "
    "*APPLIANCE REPAIRS PL* before you buy new. "
    "Professional, Reliable & Affordable.\n"
    "\n"
    "*WHY CHOOSE US?*\n"
    "\U0001F4B0 Save 70% - Repair vs Replace\n"
    "\U0001F6E1️ 3-Month Warranty on all repairs\n"
    "\U0001F697 We come to you - Home & Business calls\n"
    "\U0001F9FE Official Receipt\n"
    "\n"
    "*WE REPAIR:*\n"
    "✅ TVs - LED, LCD, Smart, Android & Projectors\n"
    "✅ Inverters - MUST, Kodak, Sako (Error 03/09, No Output)\n"
    "✅ Batteries & Solar Systems\n"
    "✅ Welding Machines & Borehole Drives\n"
    "✅ PA Systems & Amplifiers\n"
    "✅ Washing Machines, Microwaves, Electric Cookers & Ovens\n"
    "✅ All Home/Commercial Appliances\n"
    "\n"
    "\U0001F4CD Onnon Building, 88 Corner Kaguvi st & Albion St, Harare CBD\n"
    "\U0001F4DE Call / WhatsApp:\n"
    "077 521 6711 | 071 391 1390 | 073 338 6791"
)

POST_TEXTS = [POST_TEXT, POST_TEXT_V2, POST_TEXT_V3]

# Per-group ref-coded wa.me link to the repair shop's own WhatsApp number
# (+263713911390, the "071 391 1390" the ad prints). Unlike the other promos this
# is NOT the bot's number: replies go straight to the shop, which sees the ref in
# the prefilled text, so the bot's public-service-gate ad-ref recorder never sees
# them and the "AR" prefix is deliberately absent from its prefix list.
TRACKING = {
    "prefix": "AR",
    "number": "+263713911390",
    "label": "appliance repairs",
    "prefill": "Saw your post from Genaie ads (ref {ref})",
}


def main():
    return promo_broadcast.broadcast(
        text=POST_TEXTS,
        image=IMAGE_PATH,
        targets_file=TARGETS_FILE,
        state_file=STATE_FILE,
        tracking=TRACKING,
        promo_name="appliance-repairs-promo",
    )


if __name__ == "__main__":
    sys.exit(main())
