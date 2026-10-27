#!/usr/bin/env bash
set -euo pipefail

# Idempotently register the daily advertiser recruitment campaign on the ads line.
# shellcheck disable=SC1091  # sourced at runtime from the same deploy/ directory
source "$(dirname "${BASH_SOURCE[0]}")/install-promo.sh"

register_promo_cron \
  "genaie-advertisers-promo" \
  "${GENAIE_ADVERTISERS_PROMO_CRON_EXPR:-5 14 * * *}" \
  "${GENAIE_ADVERTISERS_PROMO_TZ:-Africa/Harare}" \
  "${GENAIE_ADVERTISERS_PROMO_FAILURE_ALERT_TO:-+213563719936}" \
  "/home/joseph/.openclaw/workspace/cron/genaie-advertisers-promo/post_ad.py"
