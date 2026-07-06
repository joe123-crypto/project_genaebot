#!/usr/bin/env bash
set -euo pipefail

CONTAINER="${OPENCLAW_CONTAINER:-openclaw-gateway}"
DOCKER="${DOCKER:-docker}"
NAME="job-scout-channel-health"
TZ="Africa/Harare"
ALERT_TO="${JOB_SCOUT_FAILURE_ALERT_TO:-+213563719936}"
COMMAND_ARGV='["/home/joseph/.openclaw/workspace/bin/job-scout-channel-health","--max-age-seconds","180"]'
openclaw=("$DOCKER" exec -u joseph "$CONTAINER" sh -lc 'exec env -u OPENCLAW_HOME OPENCLAW_GATEWAY_TOKEN="$GATEWAY_AUTH_TOKEN" openclaw "$@"' openclaw)

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
