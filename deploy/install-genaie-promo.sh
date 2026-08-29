#!/usr/bin/env bash
set -euo pipefail

# Registers/updates the daily Genaie-services WhatsApp ad cron on the live
# gateway. Deterministic command job (no agent/model): runs
# cron/genaie-promo/post_ad.py, which reads its bundled targets.json + image and
# sends via the gateway's live WhatsApp channel. Idempotent: edits the job if it
# already exists, else adds it.

CONTAINER="${OPENCLAW_CONTAINER:-openclaw-gateway}"
DOCKER="${DOCKER:-docker}"
NAME="genaie-promo"
CRON_EXPR="${GENAIE_PROMO_CRON_EXPR:-0 13 * * *}"
TZ="${GENAIE_PROMO_TZ:-Africa/Algiers}"
ALERT_TO="${GENAIE_PROMO_FAILURE_ALERT_TO:-+213563719936}"
SCRIPT="/home/joseph/.openclaw/workspace/cron/genaie-promo/post_ad.py"
COMMAND_ARGV="$(printf '["python3","%s"]' "$SCRIPT")"

openclaw=("$DOCKER" exec -u joseph "$CONTAINER" sh -lc 'exec env -u OPENCLAW_HOME OPENCLAW_GATEWAY_TOKEN="$GATEWAY_AUTH_TOKEN" openclaw "$@"' openclaw)

jobs_json="$("${openclaw[@]}" cron list --all --json)"
existing_id="$(printf '%s' "$jobs_json" | jq -r --arg name "$NAME" '.jobs[] | select(.name == $name) | .id' | head -1)"

if [ -n "$existing_id" ]; then
  "${openclaw[@]}" cron edit "$existing_id" \
    --name "$NAME" \
    --cron "$CRON_EXPR" \
    --tz "$TZ" \
    --exact \
    --session isolated \
    --command-argv "$COMMAND_ARGV" \
    --command-cwd /home/joseph/.openclaw/workspace \
    --timeout-seconds 600 \
    --no-deliver \
    --enable >/dev/null
else
  "${openclaw[@]}" cron add \
    --name "$NAME" \
    --cron "$CRON_EXPR" \
    --tz "$TZ" \
    --exact \
    --session isolated \
    --command-argv "$COMMAND_ARGV" \
    --command-cwd /home/joseph/.openclaw/workspace \
    --timeout-seconds 600 \
    --no-deliver \
    --json >/dev/null
fi

jobs_json="$("${openclaw[@]}" cron list --all --json)"
existing_id="$(printf '%s' "$jobs_json" | jq -r --arg name "$NAME" '.jobs[] | select(.name == $name) | .id' | head -1)"

if [ -z "$existing_id" ]; then
  printf 'ERROR: genaie-promo cron was not registered.\n' >&2
  exit 1
fi

"${openclaw[@]}" cron edit "$existing_id" \
  --failure-alert \
  --failure-alert-after 1 \
  --failure-alert-channel whatsapp \
  --failure-alert-to "$ALERT_TO" \
  --failure-alert-cooldown 1h >/dev/null

printf 'genaie-promo cron registered (%s, %s %s).\n' "$existing_id" "$CRON_EXPR" "$TZ"
