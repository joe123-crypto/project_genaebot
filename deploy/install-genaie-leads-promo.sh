#!/usr/bin/env bash
set -euo pipefail

# Registers/updates the daily Genaie lead-generation WhatsApp ad cron on the live
# gateway. Deterministic command job (no agent/model): runs
# cron/genaie-leads-promo/post_ad.py, which reads its bundled targets.json + image
# and sends via the gateway's live WhatsApp channel. This is a SEPARATE campaign
# from genaie-promo (services ad, 14:26); it runs at 17:06 Africa/Harare with its
# own image and pricing-focused copy. Idempotent: edits the job if it already
# exists, else adds it. Registration logic is shared in install-promo.sh.

# shellcheck disable=SC1091  # sourced at runtime from the same deploy/ directory
source "$(dirname "${BASH_SOURCE[0]}")/install-promo.sh"

register_promo_cron \
  "genaie-leads-promo" \
  "${GENAIE_LEADS_PROMO_CRON_EXPR:-6 17 * * *}" \
  "${GENAIE_LEADS_PROMO_TZ:-Africa/Harare}" \
  "${GENAIE_LEADS_PROMO_FAILURE_ALERT_TO:-+213563719936}" \
  "/home/joseph/.openclaw/workspace/cron/genaie-leads-promo/post_ad.py"
