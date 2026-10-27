#!/usr/bin/env bash
set -euo pipefail

# Registers/updates the daily big-player WhatsApp ad cron on the live gateway.
# Deterministic command job (no agent/model): runs
# cron/big-player-promo/post_ad.py, which reads its bundled targets.json + image
# and sends via the gateway's live WhatsApp channel. This is the operator's
# borehole/solar lead-gen ad on the second ("ads") WhatsApp line: the contact
# number in the copy is the posting account's own ({bot_number}) so replies reach
# the bot (ref prefix "BP"). Idempotent: edits the job if it already exists, else
# adds it. Registration logic is shared in install-promo.sh.

# shellcheck disable=SC1091  # sourced at runtime from the same deploy/ directory
source "$(dirname "${BASH_SOURCE[0]}")/install-promo.sh"

register_promo_cron \
  "big-player-promo" \
  "${BIG_PLAYER_PROMO_CRON_EXPR:-50 15 * * *}" \
  "${BIG_PLAYER_PROMO_TZ:-Africa/Harare}" \
  "${BIG_PLAYER_PROMO_FAILURE_ALERT_TO:-+263785632943}" \
  "/home/joseph/.openclaw/workspace/cron/big-player-promo/post_ad.py"
