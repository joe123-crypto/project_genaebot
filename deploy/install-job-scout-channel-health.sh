#!/usr/bin/env bash
set -euo pipefail

# shellcheck disable=SC1091  # sourced at runtime from the same deploy/ directory
source "$(dirname "${BASH_SOURCE[0]}")/gateway-exec.sh"

NAME="job-scout-channel-health"
TZ="Africa/Harare"
ALERT_TO="${JOB_SCOUT_FAILURE_ALERT_TO:-+213563719936}"
COMMAND_ARGV='["/home/joseph/.openclaw/workspace/bin/job-scout-channel-health","--max-age-seconds","180"]'
openclaw=("${GATEWAY_OPENCLAW[@]}")

jobs_json="$("${openclaw[@]}" cron list --all --json)"
existing_id="$(printf '%s' "$jobs_json" | jq -r --arg name "$NAME" '.jobs[] | select(.name == $name) | .id' | head -1)"
if [ -n "$existing_id" ]; then
  "${openclaw[@]}" cron edit "$existing_id" \
    --name "$NAME" --cron '*/15 * * * *' --tz "$TZ" --exact \
    --session isolated --command-argv "$COMMAND_ARGV" \
    --command-cwd /home/joseph/.openclaw/workspace --timeout-seconds 30 \
    --no-deliver --enable >/dev/null
else
  "${openclaw[@]}" cron add \
    --name "$NAME" --cron '*/15 * * * *' --tz "$TZ" --exact \
    --session isolated --command-argv "$COMMAND_ARGV" \
    --command-cwd /home/joseph/.openclaw/workspace --timeout-seconds 30 \
    --no-deliver --json >/dev/null
  jobs_json="$("${openclaw[@]}" cron list --all --json)"
  existing_id="$(printf '%s' "$jobs_json" | jq -r --arg name "$NAME" '.jobs[] | select(.name == $name) | .id' | head -1)"
fi

"${openclaw[@]}" cron edit "$existing_id" \
  --failure-alert --failure-alert-after 1 --failure-alert-channel whatsapp \
  --failure-alert-to "$ALERT_TO" --failure-alert-cooldown 1h >/dev/null
printf 'Job Scout channel listener health check scheduled every 15 minutes.\n'
