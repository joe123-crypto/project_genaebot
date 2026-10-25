#!/usr/bin/env bash
set -euo pipefail

# Registers/updates the daily industrial-lighting WhatsApp ad cron on the live
# gateway. Deterministic command job (no agent/model): runs
# cron/industrial-lighting-promo/post_ad.py, which reads its bundled targets.json
# + image and sends via the gateway's live WhatsApp channel. This is a paid
# third-party solar ad (Industrial Lighting Pvt Ltd) on the second ("ads")
# WhatsApp line: the contact number in the copy is the posting account's own
# ({bot_number}) so replies reach the bot (ref prefix "IL"). Idempotent: edits the
# job if it already exists, else adds it. Registration logic is shared in
# install-promo.sh.

# shellcheck disable=SC1091  # sourced at runtime from the same deploy/ directory
source "$(dirname "${BASH_SOURCE[0]}")/install-promo.sh"

register_promo_cron \
  "industrial-lighting-promo" \
  "${INDUSTRIAL_LIGHTING_PROMO_CRON_EXPR:-7 9 * * *}" \
  "${INDUSTRIAL_LIGHTING_PROMO_TZ:-Africa/Harare}" \
  "${INDUSTRIAL_LIGHTING_PROMO_FAILURE_ALERT_TO:-+263785632943}" \
  "/home/joseph/.openclaw/workspace/cron/industrial-lighting-promo/post_ad.py"
