#!/usr/bin/env python3
"""
CV-business daily ad poster.

Posts a FIXED, predefined advert (caption + image) for the Genaie CV writing /
revamping service to a configured set of WhatsApp groups, once per day per group.
There is NO AI/LLM generation involved: the post text is a hardcoded constant and
the image is a bundled asset. This script is meant to run from an OpenClaw
`--command` cron (deterministic, no agent turn).

Delivery, invite/JID resolution, per-group state, the permission-error skip, and
the optional per-group weekly cadence all live in the shared
`workspace/lib/promo_broadcast.py` engine (identical across the genaie-promo and
building-materials-promo crons); this script only supplies the advert text,
image(s), and file paths.

Targets come from targets.json (JIDs and/or invite links). Invite links are
resolved to group JIDs via the OpenClaw WhatsApp directory; the bot must already
be a MEMBER of each group for resolution and sending to work. Resolved JIDs are
cached in the state file to avoid re-resolving every day.

Most groups (the flat "groups" list) get the ad every day. A group may instead be
given a reduced cadence by listing it under "weekly_groups" in targets.json, which
maps a group JID to the weekdays it should be posted to (e.g. Vacancy Lamp twice a
week). The cron still runs daily; a weekly-limited group is simply skipped on days
outside its configured weekdays, so no second cron is needed.

Usage:
  post_cv_ad.py            normal run (resolve, skip groups already sent today, send)
  post_cv_ad.py --dry-run  show resolved targets, each group's caption variant +
                           ref, and every caption variant; no send, no state
                           mutation
  post_cv_ad.py --refs     print ref code -> promo + group JID + link (decode
                           refs); no send, no state mutation

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
    "CV_PROMO_TARGETS", os.path.join(HERE, "targets.json")
)
# Bundled ad images that rotate daily: the shared engine picks one by today's
# ordinal modulo the list length, so a group never sees the same image two days
# running. CV_PROMO_IMAGE overrides the whole list (a single explicit path) for
# testing. Add more assets to lengthen the rotation.
_IMAGE_ASSETS = [
    os.path.join(HERE, "assets", "cv-ad.png"),
    os.path.join(HERE, "assets", "cv-ad-2.png"),
    os.path.join(HERE, "assets", "cv-ad-3.jpg"),
]
IMAGE = os.environ.get("CV_PROMO_IMAGE") or _IMAGE_ASSETS
STATE_FILE = os.environ.get(
    "CV_PROMO_STATE",
    "/home/joseph/.openclaw/runtime/cron-state/cv-promo/state.json",
)

# Fixed advert. WhatsApp bold uses *single* asterisks, so the closing line is
# wrapped in single asterisks to render bold (not literal ** ). Everything else is
# verbatim.
POST_TEXT = (
    "Looking for a job but your CV doesn't look professional enough?\n"
    "\n"
    "We help job seekers create and revamp CVs that clearly present their "
    "skills, experience, and achievements.\n"
    "\n"
    "\U0001F4C4 CV Writing\n"
    "\U0001F504 CV Revamping\n"
    "\U0001F4BC Professional presentation\n"
    "\n"
    "\U0001F4B5 Only $5 (EcoCash)\n"
    "\U0001F4C1 For samples, see the catalogue\n"
    "\n"
    "\U0001F4F2 {bot_number}\n"
    "\U0001F310 genaie.site\n"
    "\n"
    "*Your next opportunity starts with a better CV.*"
)

# Caption variants that rotate per group per day (see promo_broadcast) so the ad
# doesn't read as copy-paste spam. Variant 1 is POST_TEXT verbatim; the others
# reword/reorder only. Every fact (services, $5 EcoCash price, catalogue samples,
# number, site) must stay IDENTICAL across variants -- never add a claim here.
POST_TEXT_V2 = (
    "Is your CV holding you back? \U0001F914\n"
    "\n"
    "Get a CV that clearly presents your skills, experience, and achievements.\n"
    "\n"
    "\U0001F4C4 CV Writing\n"
    "\U0001F504 CV Revamping\n"
    "\U0001F4BC Professional presentation\n"
    "\n"
    "\U0001F4B5 Only $5 (EcoCash)\n"
    "\U0001F4C1 Samples are in the catalogue\n"
    "\n"
    "\U0001F4F2 {bot_number}\n"
    "\U0001F310 genaie.site\n"
    "\n"
    "*Your next opportunity starts with a better CV.*"
)

POST_TEXT_V3 = (
    "\U0001F4BC *Job hunting? Start with a professional CV.*\n"
    "\n"
    "We write new CVs and revamp old ones so your skills, experience, and "
    "achievements stand out.\n"
    "\n"
    "\U0001F4B5 Only $5 (EcoCash)\n"
    "\n"
    "\U0001F4C4 CV Writing\n"
    "\U0001F504 CV Revamping\n"
    "\U0001F4BC Professional presentation\n"
    "\n"
    "\U0001F4C1 See the catalogue for samples\n"
    "\U0001F4F2 {bot_number}\n"
    "\U0001F310 genaie.site"
)

POST_TEXTS = [POST_TEXT, POST_TEXT_V2, POST_TEXT_V3]

# Per-group ref-coded wa.me link to the configured contact account.
# {bot_number} and the link use defaults.contact_account when selected;
# otherwise they use the posting account. Replies reach the bot's ad-ref recorder.
TRACKING = {
    "prefix": "CV",
    "label": "CV",
}


def main():
    return promo_broadcast.broadcast(
        text=POST_TEXTS,
        image=IMAGE,
        targets_file=TARGETS_FILE,
        state_file=STATE_FILE,
        tracking=TRACKING,
        promo_name="cv-promo",
    )


if __name__ == "__main__":
    sys.exit(main())
