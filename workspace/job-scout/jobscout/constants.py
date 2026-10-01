"""Shared Job Scout configuration constants.

Pure data (no side effects beyond reading env at import): safe for every other
jobscout submodule and job_scout.py to import.
"""

from __future__ import annotations

from .config import (
  DEFAULT_WEB_APPLY_URL,
  GENAIE_MAIL_BIN,
  OPENCLAW_BIN_FALLBACKS,
  RUNTIME_ROOT,
  WORKSPACE_ROOT,
)


WORKSPACE = WORKSPACE_ROOT
DEFAULT_STORAGE_ROOT = RUNTIME_ROOT / "users"
DEFAULT_DISPATCH_ROOT = RUNTIME_ROOT / "dispatch-runs"
DEFAULT_GROUP_FEED_ROOT = RUNTIME_ROOT / "group-feed"
DEFAULT_AGENT_MATCH_THRESHOLD = 51
DISPATCH_STATUSES = {
  "applied",
  "pending_approval",
  "action_required",
  "physical_submission",
  "no_matches",
  "failed",
  "preview",
}
TERMINAL_APPLICATION_STATUSES = {
  "applied",
  "pending_approval",
  "approved",
  "skipped",
  "action_required",
  "physical_submission",
}
MAX_APPLICATION_ATTEMPTS = 3
CANDIDATE_DESCRIPTION_CHAR_LIMIT = 4000
UNKNOWN_EMPLOYER = "Unknown employer"
UNKNOWN_ROLE = "Unknown role"
WHATSAPP_CHANNEL_SOURCES = ("zimlinks-whatsapp", "zim-job-vacancies-whatsapp", "everyday-vacancies-whatsapp")
CV_URL_LOOKUP_ATTEMPTS = 3
CV_URL_LOOKUP_RETRY_SECONDS = 2
DEFAULT_JSEARCH_COUNTRY = "dz"
DEFAULT_JSEARCH_LANGUAGE = "en"
DEFAULT_JSEARCH_DATE_POSTED = "month"
DEFAULT_OPENWEBNINJA_BASE_URL = "https://api.openwebninja.com/jsearch"
DEFAULT_RAPIDAPI_BASE_URL = "https://jsearch.p.rapidapi.com"
DEFAULT_RAPIDAPI_HOST = "jsearch.p.rapidapi.com"
JSEARCH_REQUEST_TIMEOUT = 20
JSEARCH_REQUEST_ATTEMPTS = 2
JSEARCH_RETRY_SECONDS = 2
PUBLIC_DISPATCH_FAILURE_MESSAGE = (
  "Job Scout could not finish today's job check because the service failed. "
  "I recorded it and will try again on the next run."
)
DEFAULT_GROUP_FEED_INVITE_URL = "https://chat.whatsapp.com/JZvh9lwFOvc3N2sSs8F1QD"
DEFAULT_GROUP_FEED_CACHE_MAX_AGE_SECONDS = 6 * 60 * 60
DEFAULT_GROUP_FEED_LIMIT = 80
DEFAULT_GROUP_FEED_MAX_MESSAGES = 5
DEFAULT_GROUP_FEED_SEND_DELAY_SECONDS = 3.0
