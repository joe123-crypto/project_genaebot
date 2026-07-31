#!/usr/bin/env bash
set -euo pipefail

CONTAINER="${OPENCLAW_CONTAINER:-openclaw-gateway}"
DOCKER="${DOCKER:-docker}"
NAME="job-scout-cv-conversion"
CRON_EXPR="0 * * * * *"
TZ="Africa/Algiers"
ALERT_TO="${JOB_SCOUT_FAILURE_ALERT_TO:-+213563719936}"
COMMAND_ARGV='["/home/joseph/.openclaw/workspace/bin/job-scout-convert-pending"]'
openclaw=("$DOCKER" exec -u joseph "$CONTAINER" sh -lc 'exec env -u OPENCLAW_HOME OPENCLAW_GATEWAY_TOKEN="$GATEWAY_AUTH_TOKEN" openclaw "$@"' openclaw)

jobs_json="$("${openclaw[@]}" cron list --all --json)"
existing_id="$(printf '%s' "$jobs_json" | jq -r --arg name "$NAME" '.jobs[] | select(.name == $name) | .id' | head -1)"

if [ -n "$existing_id" ]; then
  "${openclaw[@]}" cron edit "$existing_id" \
    --name "$NAME" --cron "$CRON_EXPR" --tz "$TZ" --exact \
    --session isolated --command-argv "$COMMAND_ARGV" \
    --command-cwd /home/joseph/.openclaw/workspace --timeout-seconds 600 \
    --no-deliver --enable >/dev/null
else
  "${openclaw[@]}" cron add \
    --name "$NAME" --cron "$CRON_EXPR" --tz "$TZ" --exact \
    --session isolated --command-argv "$COMMAND_ARGV" \
    --command-cwd /home/joseph/.openclaw/workspace --timeout-seconds 600 \
    --no-deliver --json >/dev/null
  jobs_json="$("${openclaw[@]}" cron list --all --json)"
  existing_id="$(printf '%s' "$jobs_json" | jq -r --arg name "$NAME" '.jobs[] | select(.name == $name) | .id' | head -1)"
fi

[ -n "$existing_id" ] || { printf 'ERROR: OpenClaw did not return the CV conversion cron id.\n' >&2; exit 1; }

"${openclaw[@]}" cron edit "$existing_id" \
  --failure-alert --failure-alert-after 1 --failure-alert-channel whatsapp \
  --failure-alert-to "$ALERT_TO" --failure-alert-cooldown 1h >/dev/null

"${openclaw[@]}" cron list --all --json | jq -e \
  --arg name "$NAME" --arg expr "$CRON_EXPR" --arg tz "$TZ" --argjson argv "$COMMAND_ARGV" \
  '.jobs[] | select(
    .name == $name and
    .enabled == true and
    .schedule.expr == $expr and
    .schedule.tz == $tz and
    .payload.argv == $argv and
    .payload.timeoutSeconds == 600 and
    .failureAlert.after == 1
  )' >/dev/null

printf 'Job Scout pending PDF conversion scheduled every minute.\n'
