"""Job feed constants. Pure data: safe for every module to import."""

from __future__ import annotations

from .config import RUNTIME_ROOT


DEFAULT_GROUP_FEED_ROOT = RUNTIME_ROOT / "group-feed"
CANDIDATE_DESCRIPTION_CHAR_LIMIT = 4000
UNKNOWN_EMPLOYER = "Unknown employer"
UNKNOWN_ROLE = "Unknown role"
DEFAULT_GROUP_FEED_INVITE_URL = "https://chat.whatsapp.com/JZvh9lwFOvc3N2sSs8F1QD"
DEFAULT_GROUP_FEED_CACHE_MAX_AGE_SECONDS = 6 * 60 * 60
DEFAULT_GROUP_FEED_LIMIT = 80
DEFAULT_GROUP_FEED_MAX_MESSAGES = 5
# Gap between group-feed sends is drawn at random from [min, max] so the posts
# do not land on a machine-regular cadence (see promo_broadcast, #108).
DEFAULT_GROUP_FEED_SEND_DELAY_SECONDS = 20.0
DEFAULT_GROUP_FEED_SEND_DELAY_MAX_SECONDS = 60.0
