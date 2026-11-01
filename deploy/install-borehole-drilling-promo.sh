#!/usr/bin/env bash
set -euo pipefail

# Registers/updates the daily borehole-drilling WhatsApp ad cron on the live
# gateway. Deterministic command job (no agent/model): runs
# cron/borehole-drilling-promo/post_ad.py, which reads its bundled targets.json +
# rotating images and sends via the gateway's live WhatsApp channel. Idempotent:
# edits the job if it already exists, else adds it. Registration logic is shared
# in install-promo.sh.

# shellcheck disable=SC1091  # sourced at runtime from the same deploy/ directory
source "$(dirname "${BASH_SOURCE[0]}")/install-promo.sh"

# Refuse migration while any affected campaign is active, before editing a job.
bash "$(dirname "${BASH_SOURCE[0]}")/retire-borehole-promos.sh" --dry-run

register_promo_cron \
  "borehole-drilling-promo" \
  "${BOREHOLE_PROMO_CRON_EXPR:-15 7 * * *}" \
  "${BOREHOLE_PROMO_TZ:-Africa/Harare}" \
  "${BOREHOLE_PROMO_FAILURE_ALERT_TO:-+263785632943}" \
  "/home/joseph/.openclaw/workspace/cron/borehole-drilling-promo/post_ad.py"

# Remove superseded registrations only after the retained job verifies successfully.
bash "$(dirname "${BASH_SOURCE[0]}")/retire-borehole-promos.sh"
