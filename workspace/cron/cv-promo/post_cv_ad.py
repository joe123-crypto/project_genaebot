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
  post_cv_ad.py --dry-run  show resolved targets + the message; no send, no state
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
SEND_DELAY_SECONDS = float(os.environ.get("CV_PROMO_SEND_DELAY", "3"))

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
    "\U0001F4F2 +263785632943\n"
    "\U0001F310 genaie.site\n"
    "\n"
    "*Your next opportunity starts with a better CV.*"
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
