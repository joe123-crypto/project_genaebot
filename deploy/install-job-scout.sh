#!/usr/bin/env bash
set -euo pipefail

# shellcheck disable=SC1091  # sourced at runtime from the same deploy/ directory
source "$(dirname "${BASH_SOURCE[0]}")/gateway-exec.sh"

NAME="job-scout"
LEGACY_NAME="job-scout-pilot"
CRON_EXPR="0 8 * * *"
TZ="Africa/Harare"
ALERT_TO="${JOB_SCOUT_FAILURE_ALERT_TO:-+213563719936}"
COMMAND_ARGV='["/home/joseph/.openclaw/workspace/bin/job-scout-agent","--daily-live"]'
INSTALL_STATE="preserve"
case "${1:-}" in
  "") ;;
  --enable) INSTALL_STATE="enabled" ;;
  --disable) INSTALL_STATE="disabled" ;;
  *) printf 'usage: %s [--enable|--disable]\n' "$0" >&2; exit 2 ;;
esac
job_scout=("${GATEWAY_EXEC[@]}" /home/joseph/.openclaw/workspace/bin/job-scout)
openclaw=("${GATEWAY_OPENCLAW[@]}")

# The job-scout-browser worker handles URL-only applications. It is a separate
# container, so the token-parity preflight below only applies where that
# container can exist -- i.e. a local Docker runtime. Production (Railway) runs
# the gateway service alone and has no browser worker, so URL-only applications
# are unavailable there and the dispatcher records them as action_required.
if [ "$GATEWAY_EXEC_MODE" = docker ]; then
  docker_bin="${DOCKER:-docker}"
  container="${OPENCLAW_CONTAINER:-openclaw-gateway}"

  if ! "$docker_bin" exec "$container" sh -lc '[ "${#JOB_SCOUT_BROWSER_TOKEN}" -ge 32 ]'; then
    printf 'ERROR: JOB_SCOUT_BROWSER_TOKEN is missing from the gateway or shorter than 32 characters.\n' >&2
    exit 1
  fi

  browser_id="$("$docker_bin" ps --filter label=com.docker.compose.service=job-scout-browser --format '{{.ID}}' | head -1)"
  if [ -z "$browser_id" ]; then
    printf 'ERROR: the optional job-scout-browser container is not running.\n' >&2
    exit 1
  fi
  gateway_token_hash="$("$docker_bin" exec "$container" sh -lc 'printf %s "$JOB_SCOUT_BROWSER_TOKEN" | sha256sum | cut -d " " -f 1')"
  browser_token_hash="$("$docker_bin" exec "$browser_id" sh -lc 'printf %s "$JOB_SCOUT_BROWSER_TOKEN" | sha256sum | cut -d " " -f 1')"
  if [ "$gateway_token_hash" != "$browser_token_hash" ]; then
    printf 'ERROR: gateway and browser worker tokens do not match.\n' >&2
    exit 1
  fi
  unset gateway_token_hash browser_token_hash docker_bin container
else
  printf 'NOTE: no browser worker on this runtime; URL-only applications stay action_required.\n' >&2
fi

set +e
runtime_check_output="$("${job_scout[@]}" check-runtime --check-browser --check-subscribers 2>&1)"
runtime_check_rc=$?
set -e
if [ "$runtime_check_rc" -ne 0 ]; then
  if printf '%s\n' "$runtime_check_output" | grep -Eqi 'RESOURCE_EXHAUSTED|quota exceeded'; then
    printf '[WARN] Agent Genaie Firestore quota is exhausted; Job Scout schedule reconciliation was deferred and the existing schedule was not changed.\n' >&2
    exit 75
  fi
  printf '%s\n' "$runtime_check_output" >&2
  exit "$runtime_check_rc"
fi
printf '%s\n' "$runtime_check_output"

# Recreate the job so a historical agent-turn payload cannot survive an edit.
# The new job is added before the old ones are removed so a failed add leaves
# the existing schedule running; a transient duplicate is safe because
# dispatch overlap is serialized by the dispatch lock and active-run marker.
jobs_json="$("${openclaw[@]}" cron list --all --json)"
if [ "$INSTALL_STATE" = "preserve" ]; then
  existing_count="$(printf '%s' "$jobs_json" | jq --arg current "$NAME" --arg legacy "$LEGACY_NAME" '[.jobs[] | select(.name == $current or .name == $legacy)] | length')"
  existing_enabled="$(printf '%s' "$jobs_json" | jq -r --arg current "$NAME" --arg legacy "$LEGACY_NAME" '[.jobs[] | select((.name == $current or .name == $legacy) and .enabled == true)] | length > 0')"
  if [ "$existing_count" -gt 0 ] && [ "$existing_enabled" != "true" ]; then
    INSTALL_STATE="disabled"
  else
    INSTALL_STATE="enabled"
  fi
fi

add_state=()
if [ "$INSTALL_STATE" = "disabled" ]; then
  add_state=(--disabled)
fi

add_result="$("${openclaw[@]}" cron add \
  --name "$NAME" \
  --cron "$CRON_EXPR" \
  --tz "$TZ" \
  --exact \
  --session isolated \
  --command-argv "$COMMAND_ARGV" \
  --command-cwd /home/joseph/.openclaw/workspace \
  --timeout-seconds 21600 \
  --expect-final \
  --no-deliver \
  "${add_state[@]}" \
  --json)"
existing_id="$(printf '%s' "$add_result" | jq -r '.job.id // .id // empty')"
[ -n "$existing_id" ] || { printf 'ERROR: OpenClaw did not return the Job Scout cron id.\n' >&2; exit 1; }

# The snapshot predates the add, so the new job id can never appear here.
while IFS= read -r old_id; do
  [ -n "$old_id" ] || continue
  [ "$old_id" != "$existing_id" ] || continue
  "${openclaw[@]}" cron remove "$old_id" --json >/dev/null
done < <(printf '%s' "$jobs_json" | jq -r --arg current "$NAME" --arg legacy "$LEGACY_NAME" \
  '.jobs[] | select(.name == $current or .name == $legacy) | .id')

"${openclaw[@]}" cron edit "$existing_id" \
  --failure-alert \
  --failure-alert-after 1 \
  --failure-alert-channel whatsapp \
  --failure-alert-to "$ALERT_TO" \
  --failure-alert-cooldown 1h >/dev/null

expected_enabled=true
[ "$INSTALL_STATE" = "disabled" ] && expected_enabled=false
"${openclaw[@]}" cron list --all --json | jq -e --arg name "$NAME" --argjson expected_enabled "$expected_enabled" \
  '.jobs[] | select(
    .name == $name and
    .enabled == $expected_enabled and
    .sessionTarget == "isolated" and
    .schedule.expr == "0 8 * * *" and
    .schedule.tz == "Africa/Harare" and
    .payload.kind == "command" and
    .payload.argv == ["/home/joseph/.openclaw/workspace/bin/job-scout-agent", "--daily-live"] and
    .payload.cwd == "/home/joseph/.openclaw/workspace" and
    .payload.timeoutSeconds == 21600 and
    .failureAlert.after == 1
  )' >/dev/null

printf 'Job Scout OpenClaw agent scheduled daily at 08:00 Africa/Harare for all ready subscribers (state=%s, four concurrent children).\n' "$INSTALL_STATE"
