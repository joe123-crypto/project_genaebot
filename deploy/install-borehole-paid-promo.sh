#!/usr/bin/env bash
set -euo pipefail

# Registers/updates the daily borehole-paid WhatsApp ad cron on the live gateway.
# Deterministic command job (no agent/model): runs
# cron/borehole-paid-promo/post_ad.py, which reads its bundled targets.json +
# image and sends via the gateway's live WhatsApp channel. This is a PAID ADVERT
# the operator runs for a third-party borehole driller, kept separate from the
# operator's own borehole-drilling-promo. Idempotent: edits the job if it already
# exists, else adds it. Registration logic is shared in install-promo.sh.

# shellcheck disable=SC1091  # sourced at runtime from the same deploy/ directory
source "$(dirname "${BASH_SOURCE[0]}")/install-promo.sh"

register_promo_cron \
  "borehole-paid-promo" \
  "${BOREHOLE_PAID_PROMO_CRON_EXPR:-15 7 * * *}" \
  "${BOREHOLE_PAID_PROMO_TZ:-Africa/Harare}" \
  "${BOREHOLE_PAID_PROMO_FAILURE_ALERT_TO:-+263785632943}" \
  "/home/joseph/.openclaw/workspace/cron/borehole-paid-promo/post_ad.py"
