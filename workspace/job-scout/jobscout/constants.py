"""Shared Job Scout configuration constants.

Pure data (no side effects beyond reading env at import): safe for every other
jobscout submodule and job_scout.py to import.
"""

from __future__ import annotations

import os
from pathlib import Path


WORKSPACE = Path("/home/joseph/.openclaw/workspace")
RUNTIME_ROOT = Path(os.environ.get("JOB_SCOUT_RUNTIME_ROOT", "/home/joseph/.openclaw/runtime/job-scout"))
DEFAULT_STORAGE_ROOT = RUNTIME_ROOT / "users"
DEFAULT_DISPATCH_ROOT = RUNTIME_ROOT / "dispatch-runs"
DISPATCH_STATUSES = {"applied", "action_required", "physical_submission", "no_matches", "failed"}
TERMINAL_APPLICATION_STATUSES = {"applied", "skipped", "action_required", "physical_submission"}
MAX_APPLICATION_ATTEMPTS = 3
CV_URL_LOOKUP_ATTEMPTS = 3
CV_URL_LOOKUP_RETRY_SECONDS = 2
DEFAULT_WEB_APPLY_URL = "http://job-scout-browser:8787/apply"
DEFAULT_JSEARCH_COUNTRY = "dz"
DEFAULT_JSEARCH_LANGUAGE = "fr"
DEFAULT_JSEARCH_DATE_POSTED = "3days"
DEFAULT_OPENWEBNINJA_BASE_URL = "https://api.openwebninja.com/jsearch"
DEFAULT_RAPIDAPI_BASE_URL = "https://jsearch.p.rapidapi.com"
DEFAULT_RAPIDAPI_HOST = "jsearch.p.rapidapi.com"
GENAIE_MAIL_BIN = Path("/home/joseph/.openclaw/workspace/bin/genaie-mail")
OPENCLAW_BIN_FALLBACKS = (
  Path("/usr/local/bin/openclaw"),
  Path("/home/joseph/.npm-global/bin/openclaw"),
)
PUBLIC_DISPATCH_FAILURE_MESSAGE = (
  "Job Scout could not finish today's job check because the service failed. "
  "I recorded it and will try again on the next run."
)
