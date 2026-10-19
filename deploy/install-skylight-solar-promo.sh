#!/usr/bin/env bash
set -euo pipefail

# Registers/updates the daily skylight-solar WhatsApp ad cron on the live gateway.
# Deterministic command job (no agent/model): runs
# cron/skylight-solar-promo/post_ad.py, which reads its bundled targets.json +
# rotating images and sends via the gateway's live WhatsApp channel. Idempotent:
# edits the job if it already exists, else adds it. Registration logic is shared
# in install-promo.sh.

# shellcheck disable=SC1091  # sourced at runtime from the same deploy/ directory
source "$(dirname "${BASH_SOURCE[0]}")/install-promo.sh"

register_promo_cron \
  "skylight-solar-promo" \
  "${SKYLIGHT_SOLAR_PROMO_CRON_EXPR:-17 9 * * *}" \
  "${SKYLIGHT_SOLAR_PROMO_TZ:-Africa/Harare}" \
  "${SKYLIGHT_SOLAR_PROMO_FAILURE_ALERT_TO:-+263785632943}" \
  "/home/joseph/.openclaw/workspace/cron/skylight-solar-promo/post_ad.py"
