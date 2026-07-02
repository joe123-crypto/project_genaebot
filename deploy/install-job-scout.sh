#!/usr/bin/env bash
set -euo pipefail

CONTAINER="${OPENCLAW_CONTAINER:-openclaw-gateway}"
DOCKER="${DOCKER:-docker}"
NAME="job-scout"
LEGACY_NAME="job-scout-pilot"
CRON_EXPR="0 9 * * *"
TZ="Africa/Harare"
ALERT_TO="${JOB_SCOUT_FAILURE_ALERT_TO:-+213563719936}"
COMMAND_ARGV='["/home/joseph/.openclaw/workspace/bin/job-scout","dispatch-jsearch","--limit-ready","100"]'

job_scout=("$DOCKER" exec -u joseph "$CONTAINER" /home/joseph/.openclaw/workspace/bin/job-scout)
openclaw=("$DOCKER" exec -u joseph "$CONTAINER" sh -lc 'exec env -u OPENCLAW_HOME OPENCLAW_GATEWAY_TOKEN="$GATEWAY_AUTH_TOKEN" openclaw "$@"' openclaw)

if ! "$DOCKER" exec "$CONTAINER" sh -lc '[ "${#JOB_SCOUT_BROWSER_TOKEN}" -ge 32 ]'; then
  printf 'ERROR: JOB_SCOUT_BROWSER_TOKEN is missing from the gateway or shorter than 32 characters.\n' >&2
  exit 1
fi

browser_id="$("$DOCKER" ps --filter label=com.docker.compose.service=job-scout-browser --format '{{.ID}}' | head -1)"
if [ -z "$browser_id" ]; then
  printf 'ERROR: the optional job-scout-browser container is not running.\n' >&2
  exit 1
fi
gateway_token_hash="$("$DOCKER" exec "$CONTAINER" sh -lc 'printf %s "$JOB_SCOUT_BROWSER_TOKEN" | sha256sum | cut -d " " -f 1')"
browser_token_hash="$("$DOCKER" exec "$browser_id" sh -lc 'printf %s "$JOB_SCOUT_BROWSER_TOKEN" | sha256sum | cut -d " " -f 1')"
if [ "$gateway_token_hash" != "$browser_token_hash" ]; then
  printf 'ERROR: gateway and browser worker tokens do not match.\n' >&2
  exit 1
fi
unset gateway_token_hash browser_token_hash

"${job_scout[@]}" check-runtime --check-browser --check-subscribers

jobs_json="$("${openclaw[@]}" cron list --all --json)"
existing_id="$(printf '%s' "$jobs_json" | jq -r --arg name "$NAME" '.jobs[] | select(.name == $name) | .id' | head -1)"
if [ -z "$existing_id" ]; then
  existing_id="$(printf '%s' "$jobs_json" | jq -r --arg name "$LEGACY_NAME" '.jobs[] | select(.name == $name) | .id' | head -1)"
fi

if [ -n "$existing_id" ]; then
  "${openclaw[@]}" cron edit "$existing_id" \
    --name "$NAME" \
    --cron "$CRON_EXPR" \
    --tz "$TZ" \
    --exact \
    --session isolated \
    --command-argv "$COMMAND_ARGV" \
    --command-cwd /home/joseph/.openclaw/workspace \
    --timeout-seconds 1200 \
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
    --timeout-seconds 1200 \
    --no-deliver \
    --json >/dev/null
fi

jobs_json="$("${openclaw[@]}" cron list --all --json)"
existing_id="$(printf '%s' "$jobs_json" | jq -r --arg name "$NAME" '.jobs[] | select(.name == $name) | .id' | head -1)"

# Remove any duplicate legacy schedule after the canonical job is installed.
while IFS= read -r legacy_id; do
  [ -n "$legacy_id" ] || continue
  [ "$legacy_id" = "$existing_id" ] && continue
  "${openclaw[@]}" cron remove "$legacy_id" --json >/dev/null
done < <(printf '%s' "$jobs_json" | jq -r --arg name "$LEGACY_NAME" '.jobs[] | select(.name == $name) | .id')

"${openclaw[@]}" cron edit "$existing_id" \
  --failure-alert \
  --failure-alert-after 1 \
  --failure-alert-channel whatsapp \
  --failure-alert-to "$ALERT_TO" \
  --failure-alert-cooldown 1h >/dev/null

"${openclaw[@]}" cron list --all --json | jq -e --arg name "$NAME" \
  '.jobs[] | select(
    .name == $name and
    .enabled == true and
    .schedule.expr == "0 9 * * *" and
    .schedule.tz == "Africa/Harare" and
    .payload.argv == ["/home/joseph/.openclaw/workspace/bin/job-scout", "dispatch-jsearch", "--limit-ready", "100"] and
    .failureAlert.after == 1
  )' >/dev/null

printf 'Job Scout scheduled daily at 09:00 Africa/Harare for up to 100 ready subscribers.\n'
