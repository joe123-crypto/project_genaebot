#!/usr/bin/env bash
set -euo pipefail

# shellcheck disable=SC1091  # sourced at runtime from the same deploy/ directory
source "$(dirname "${BASH_SOURCE[0]}")/gateway-exec.sh"

NAME="job-scout-approvals"
CRON_EXPR="0 */5 * * * *"
TZ="Africa/Algiers"
ALERT_TO="${JOB_SCOUT_FAILURE_ALERT_TO:-+213563719936}"
COMMAND_ARGV='["/home/joseph/.openclaw/workspace/bin/job-scout-agent","--approved-live"]'
openclaw=("${GATEWAY_OPENCLAW[@]}")

jobs_json="$("${openclaw[@]}" cron list --all --json)"
existing_id="$(printf '%s' "$jobs_json" | jq -r --arg name "$NAME" '.jobs[] | select(.name == $name) | .id' | head -1)"

if [ -n "$existing_id" ]; then
  "${openclaw[@]}" cron edit "$existing_id" \
    --name "$NAME" --cron "$CRON_EXPR" --tz "$TZ" --exact \
    --session isolated --command-argv "$COMMAND_ARGV" \
    --command-cwd /home/joseph/.openclaw/workspace --timeout-seconds 21600 \
    --expect-final --no-deliver --enable >/dev/null
else
  "${openclaw[@]}" cron add \
    --name "$NAME" --cron "$CRON_EXPR" --tz "$TZ" --exact \
    --session isolated --command-argv "$COMMAND_ARGV" \
    --command-cwd /home/joseph/.openclaw/workspace --timeout-seconds 21600 \
    --expect-final --no-deliver --json >/dev/null
  jobs_json="$("${openclaw[@]}" cron list --all --json)"
  existing_id="$(printf '%s' "$jobs_json" | jq -r --arg name "$NAME" '.jobs[] | select(.name == $name) | .id' | head -1)"
fi

[ -n "$existing_id" ] || { printf 'ERROR: OpenClaw did not return the approval worker cron id.\n' >&2; exit 1; }

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
    .payload.timeoutSeconds == 21600 and
    .failureAlert.after == 1
  )' >/dev/null

printf 'Job Scout approved-application worker scheduled every 5 minutes.\n'
