#!/usr/bin/env python3
"""Manage Job Scout setup through Agent Genaie's internal API."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from country_codes import country_code, country_name

from jobscout.constants import (
  CV_URL_LOOKUP_ATTEMPTS,
  CV_URL_LOOKUP_RETRY_SECONDS,
  DEFAULT_DISPATCH_ROOT,
  DEFAULT_GROUP_FEED_CACHE_MAX_AGE_SECONDS,
  DEFAULT_GROUP_FEED_INVITE_URL,
  DEFAULT_GROUP_FEED_LIMIT,
  DEFAULT_GROUP_FEED_MAX_MESSAGES,
  DEFAULT_GROUP_FEED_ROOT,
  DEFAULT_GROUP_FEED_SEND_DELAY_SECONDS,
  DEFAULT_JSEARCH_COUNTRY,
  DEFAULT_JSEARCH_DATE_POSTED,
  DEFAULT_JSEARCH_LANGUAGE,
  DEFAULT_OPENWEBNINJA_BASE_URL,
  DEFAULT_RAPIDAPI_BASE_URL,
  DEFAULT_RAPIDAPI_HOST,
  DEFAULT_STORAGE_ROOT,
  DEFAULT_WEB_APPLY_URL,
  DISPATCH_STATUSES,
  GENAIE_MAIL_BIN,
  JSEARCH_RETRY_SECONDS,
  MAX_APPLICATION_ATTEMPTS,
  OPENCLAW_BIN_FALLBACKS,
  PUBLIC_DISPATCH_FAILURE_MESSAGE,
  RUNTIME_ROOT,
  TERMINAL_APPLICATION_STATUSES,
  UNKNOWN_EMPLOYER,
  UNKNOWN_ROLE,
  WHATSAPP_CHANNEL_SOURCES,
  WORKSPACE,
)

os.environ.setdefault(
  "OPENCLAW_PENDING_LINKS_PATH",
  "/home/joseph/.openclaw/runtime/account-link/pending-links.json",
)
ACCOUNT_LINK_DIR = WORKSPACE / "account-link"
if str(ACCOUNT_LINK_DIR) not in sys.path:
  sys.path.insert(0, str(ACCOUNT_LINK_DIR))

LIB_DIR = str(WORKSPACE / "lib")
if LIB_DIR not in sys.path:
  sys.path.insert(0, LIB_DIR)

from genaie_env import (
  agent_base_url,
  agent_base_url_source,
  api_key,
  endpoint,
  env_default,
)
from pending_cache import (
  criteria as pending_criteria,
  get_pending_link,
  remove_pending_links,
  store_pending_link,
  utc_now_iso,
)

# Re-export the established helper surface for scripts and integrations that
# import job_scout. Implementations call dependencies through their owning
# jobscout modules, which is also where tests patch internal collaborators.
from jobscout import api
from jobscout import dispatch_store
from jobscout import group_feed
from jobscout.subscriber import (
  application_artifact_dir,
  applicant_name,
  cv_local_dest,
  display_name,
  subscriber_cv_file,
  subscriber_phone,
  subscriber_phone_hash,
  subscriber_sender_email,
)
from jobscout.textutil import public_text
from jobscout.delivery import (
  parse_json_output,
  gmail_sender_connected,
  request_web_application,
  result_message_id,
  run_json_command,
  send_application_email,
  send_whatsapp_message,
  send_whatsapp_summary,
)
from jobscout.application import (
  action_instruction,
  applications_from_response,
  build_user_summary,
  candidate_application_url,
  candidate_attempt,
  cover_letter_text,
  match_reason,
  web_apply_payload,
  write_cover_letter,
)
from jobscout.util import (
  count_arg,
  effective_user,
  normalize_phone,
  phone_hash,
  safe_run_id,
  slug_part,
  utc_now,
)
from jobscout.profile import (
  clean_items,
  load_profile_json,
  max_applications,
  normalize_stored_locations,
  preference_code,
  profile_values,
  resolve_preferred_locations,
  resolve_profile_locations,
  resolved_profile_country,
  search_queries,
  subscriber_preferences,
  subscriber_user_id,
  user_key,
)
from jobscout.jsearch import (
  JSearchUnavailable,
  application_key,
  apply_links,
  apply_source_filter_stats,
  candidate_key,
  closing_state,
  extract_emails,
  fetch_candidate_sources,
  fetch_jsearch_candidates,
  fetch_jsearch_candidates_with_result,
  fetch_scraped_candidates,
  fetch_whatsapp_channel_candidates_with_results,
  fetch_zimlinks_candidates,
  filter_candidates,
  filter_candidates_for_user_with_stats,
  filter_candidates_with_stats,
  finalize_channel_candidate_dispatch,
  global_filter_candidates,
  jsearch_items,
  jsearch_provider,
  jsearch_request,
  is_low_information_candidate,
  normalized_candidate,
  duplicate_key,
  record_channel_candidate_outcome,
  register_channel_candidate_eligibility,
  zw_targeted,
)


# Keep this file as the stable executable and import surface used by deployment,
# operator scripts, and existing integrations. Command implementations live in
# focused jobscout modules.
from jobscout.commands_setup import (
  cmd_check_runtime,
  cmd_fetch_candidates,
  cmd_list_applications,
  cmd_list_ready,
  cmd_record_application,
  cmd_resolve_location,
  cmd_save_profile,
  cmd_stage_cv,
  cmd_status,
  resolve_cv_ref,
)
from jobscout.commands_group_feed import (
  cmd_deliver_group_feed,
  group_feed_invite_url_from_args,
  group_feed_target_from_args,
  maybe_write_group_feed_cache,
)
from jobscout.commands_dispatch import (
  cmd_finish_dispatch,
  cmd_record_dispatch_outcome,
  cmd_start_dispatch,
  cmd_verify_dispatch,
)
from jobscout.dispatch_runner import (
  browser_apply_enabled,
  cmd_dispatch_jsearch,
  dispatch_jsearch_locked,
  record_candidate_application,
  scoped_subscribers,
  subscriber_is_dispatch_ready,
)
from jobscout.agent_dispatch import (
  cmd_agent_claim,
  cmd_agent_complete,
  cmd_agent_dispatch,
  cmd_agent_fallback,
  cmd_agent_finish,
  cmd_agent_prepare,
  cmd_agent_status,
  validate_agent_result,
)
from jobscout.cli import build_parser, main


if __name__ == "__main__":
  raise SystemExit(main())
