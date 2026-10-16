#!/usr/bin/env bash
set -euo pipefail

# Registers/updates the daily building-materials WhatsApp ad cron on the live
# gateway. Deterministic command job (no agent/model): runs
# cron/building-materials-promo/post_ad.py, which reads its bundled targets.json +
# image and sends via the gateway's live WhatsApp channel. Idempotent: edits the
# job if it already exists, else adds it. Registration logic is shared in
# install-promo.sh.

# shellcheck disable=SC1091  # sourced at runtime from the same deploy/ directory
source "$(dirname "${BASH_SOURCE[0]}")/install-promo.sh"

register_promo_cron \
  "building-materials-promo" \
  "${BUILDING_MATERIALS_PROMO_CRON_EXPR:-50 12 * * *}" \
  "${BUILDING_MATERIALS_PROMO_TZ:-Africa/Harare}" \
  "${BUILDING_MATERIALS_PROMO_FAILURE_ALERT_TO:-+213563719936}" \
  "/home/joseph/.openclaw/workspace/cron/building-materials-promo/post_ad.py"
