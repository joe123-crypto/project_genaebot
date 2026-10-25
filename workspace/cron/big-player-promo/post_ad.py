#!/usr/bin/env python3
"""
Big Player borehole / solar daily ad poster.

Posts a FIXED, predefined advert (caption + image) for borehole drilling and
solar water systems to a configured set of WhatsApp groups, once per day per
group. There is NO AI/LLM generation involved: the post text is a set of
hardcoded caption variants and the image is one bundled poster. This script is
meant to run from an OpenClaw `--command` cron (deterministic, no agent turn).

The operator runs this as a lead-gen ad on the second WhatsApp line (the "ads"
account): every contact number in the advertiser-supplied copy is replaced with
the configured contact line via the {bot_number} placeholder, and the group
invite link is dropped, so replies reach the bot and the leads are attributable
under the "BP" ad-ref prefix (in the public-service-gate's AD_REF_PREFIXES) --
the same lead-gen pattern as the skylight / borehole / genaie promos.

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
    "BIG_PLAYER_TARGETS", os.path.join(HERE, "targets.json")
)
STATE_FILE = os.environ.get(
    "BIG_PLAYER_STATE",
    "/home/joseph/.openclaw/runtime/cron-state/big-player-promo/state.json",
)

# One bundled ad poster (the advertiser-supplied image). BIG_PLAYER_IMAGE
# overrides it (a single explicit path) for testing.
IMAGE = os.environ.get("BIG_PLAYER_IMAGE") or os.path.join(
    HERE, "assets", "big-player-1.jpg"
)

# Fixed advert caption. Every contact number is the configured contact line,
# substituted into {bot_number}; the advertiser's group-invite link is dropped.
# Emoji are written as explicit \u / \U escapes so the source stays ASCII-safe.
# The pricing/spec block below (_PITCH) carries ALL the digits, and the three
# rotating variants differ only in their digit-free intro, so every variant keeps
# identical "facts" (deploy/test_promo_captions_tracking.py pins that).
_PITCH = (
    "\U0001F6B0 *BOREHOLE DRILLING & INSTALLATION* (zvakazara):\n"
    "\U0001F4A6 Promotion: First 40m, class 6 — *$750* "
    "(casing ne flashing ipapo, ne ma pipes acho)\n"
    "\U0001F4A7 Extra meter: *$21* per meter (kubva pa first 40m)\n"
    "\U0001F4A7 Double casing: *$22* per m (kuvirikidza casing)\n"
    "\U0001F4A7 Survey / Siting: *$80* (ZINWA permit mu ma town chete)\n"
    "\U0001F4A7 Borehole deepening — huyai inbox\n"
    "\U0001F4A6 Borehole flushing / casing yakollapsa\n"
    "\U0001F6E0\U0000FE0F Borehole services — any problem\n"
    "\n"
    "\U00002600\U0000FE0F *SOLAR & WATER SYSTEMS:*\n"
    "\U0001F4A7 Borehole Solar Installation (ne 5000L tank ne tank stand ipapo)\n"
    "\U0001F4A7 Borehole Electrical Full Package Installation\n"
    "\U0001F3E0 Home Complete Solar System: *1,5 KVA – 12 KVA*\n"
    "\U0001F3E2 Solar Commercial System Installation\n"
    "\n"
    "\U0001F4B5 Payments are done on site — same-day service.\n"
    "\U0001F4CD Prices may vary outside Harare (zvinoenderana nenzvimbo yamuri).\n"
    "\U0001F1FF\U0001F1FC Service areas: across Zimbabwe.\n"
    "\U0001F3A7 Tine audio inotsanangura zvizere — huyai inbox tikupei.\n"
    "\n"
    "\U0001F4DE Call / WhatsApp: {bot_number}"
)

_HEADER = (
    "\U0001F327\U0000FE0F\U0001F4A7 *RAIN SEASON YASVIKA — CHENGETAI MVURA!* "
    "\U0001F4A7\U0001F327\U0000FE0F\n\n"
)

_CALLOUT = (
    "‼️ *MUKANA NDIWOYU!* Musazononoka mozoti hee komazokwidza sei.\n"
    "\U0001F911 *MITENGO TADEREDZA ZVEKARE!*\n\n"
)

POST_TEXT = (
    _HEADER
    + "Vabereki, rain season yasvika — ngatichengetei mvura! Tinokuvhiirai "
    "borehole ne solar water system yakazara pamba, paplot kana pabhizimusi.\n\n"
    + _CALLOUT
    + _PITCH
)

# Variants reword only the digit-free intro; the pitch block and {bot_number}
# stay identical so the facts never drift between variants.
POST_TEXT_V2 = (
    _HEADER
    + "Mvura yakachena yakakwana pamusha ndeyemunhu wese. Tsvagai mvura pasina "
    "kutambura — tinokuchererai borehole toisa solar pump zvakazara.\n\n"
    + _CALLOUT
    + _PITCH
)

POST_TEXT_V3 = (
    _HEADER
    + "Regai kuomerwa nemvura gore rino. Tine borehole drilling ne solar "
    "installation yamunovimba nayo — pamba, papurazi kana pabhizimusi.\n\n"
    + _CALLOUT
    + _PITCH
)

POST_TEXTS = [POST_TEXT, POST_TEXT_V2, POST_TEXT_V3]

# Per-group ref-coded wa.me link to the configured contact account.
# {bot_number} and the link use defaults.contact_account when selected;
# otherwise they use the posting account. Replies reach the bot's ad-ref recorder.
TRACKING = {
    "prefix": "BP",
    "label": "borehole",
}


def main():
    return promo_broadcast.broadcast(
        text=POST_TEXTS,
        image=IMAGE,
        targets_file=TARGETS_FILE,
        state_file=STATE_FILE,
        tracking=TRACKING,
        promo_name="big-player-promo",
    )


if __name__ == "__main__":
    sys.exit(main())
