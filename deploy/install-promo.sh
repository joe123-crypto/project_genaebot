#!/usr/bin/env bash
# Shared helper for the deterministic WhatsApp promo-ad cron installers
# (one per campaign: cv, genaie, genaie-leads, building-materials, fruit-trees,
# borehole-drilling, appliance-repairs).
#
# This file is SOURCED by those wrappers, not run directly; it exposes one
# function, register_promo_cron, which idempotently registers/updates a
# deterministic (no agent/model) command cron on the live gateway that runs the
# promo's post script and posts via the gateway's live WhatsApp channel. Each
# wrapper differs only in the cron name, schedule, timezone, failure-alert target,
# and script path, so that per-promo config stays in the wrapper and the (formerly
# copy-pasted) registration logic lives here.
#
# Usage (from a wrapper that has already `set -euo pipefail`):
#   source "$(dirname "${BASH_SOURCE[0]}")/install-promo.sh"
#   register_promo_cron <name> <cron-expr> <tz> <failure-alert-to> <script-path>
#
# The gateway access mechanism comes from gateway-exec.sh: `railway ssh` against
# the production service by default, or a local container when DOCKER /
# OPENCLAW_CONTAINER / OPENCLAW_EXEC_MODE=docker is set.

# shellcheck disable=SC1091  # sourced at runtime from the same deploy/ directory
source "$(dirname "${BASH_SOURCE[0]}")/gateway-exec.sh"

# Job timeout for every promo run. promo_broadcast spaces sends 20-60 s apart
# (random), so the largest lists (~50 groups) take ~45 min typically and ~99 min
# worst case (49 gaps x 60 s + sends); the old 600 s limit would kill them
# mid-run. 7200 s (120 min) leaves headroom for slow sends and fits promos up to
# ~60 groups at full spacing while staying clear of the next promo's slot.
# Changing this only reaches the live gateway when the promo installers are re-run.
PROMO_CRON_TIMEOUT_SECONDS="${PROMO_CRON_TIMEOUT_SECONDS:-7200}"

register_promo_cron() {
  local name="$1" cron_expr="$2" tz="$3" alert_to="$4" script="$5"
  local command_argv
  command_argv="$(printf '["python3","%s"]' "$script")"

  local openclaw
  openclaw=("${GATEWAY_OPENCLAW[@]}")

  local jobs_json existing_id
  jobs_json="$("${openclaw[@]}" cron list --all --json)"
  existing_id="$(printf '%s' "$jobs_json" | jq -r --arg name "$name" '.jobs[] | select(.name == $name) | .id' | head -1)"

  if [ -n "$existing_id" ]; then
    "${openclaw[@]}" cron edit "$existing_id" \
      --name "$name" \
      --cron "$cron_expr" \
      --tz "$tz" \
      --exact \
      --session isolated \
      --command-argv "$command_argv" \
      --command-cwd /home/joseph/.openclaw/workspace \
      --timeout-seconds "$PROMO_CRON_TIMEOUT_SECONDS" \
      --no-deliver \
      --enable >/dev/null
  else
    "${openclaw[@]}" cron add \
      --name "$name" \
      --cron "$cron_expr" \
      --tz "$tz" \
      --exact \
      --session isolated \
      --command-argv "$command_argv" \
      --command-cwd /home/joseph/.openclaw/workspace \
      --timeout-seconds "$PROMO_CRON_TIMEOUT_SECONDS" \
      --no-deliver \
      --json >/dev/null
  fi

  jobs_json="$("${openclaw[@]}" cron list --all --json)"
  existing_id="$(printf '%s' "$jobs_json" | jq -r --arg name "$name" '.jobs[] | select(.name == $name) | .id' | head -1)"

  if [ -z "$existing_id" ]; then
    printf 'ERROR: %s cron was not registered.\n' "$name" >&2
    return 1
  fi

  "${openclaw[@]}" cron edit "$existing_id" \
    --failure-alert \
    --failure-alert-after 1 \
    --failure-alert-channel whatsapp \
    --failure-alert-to "$alert_to" \
    --failure-alert-cooldown 1h >/dev/null

  # Prove the schedule and job timeout actually landed on the volume's cron
  # state. The run spacing in promo_broadcast reads the timeout back at run
  # time, so a stale one must fail loudly here instead of quietly tightening
  # every run; and because the schedule is the thing these installers exist to
  # change (a merge to main never re-registers a cron), a stale --cron/--tz has
  # to fail here too -- nothing else would catch it.
  if ! "${openclaw[@]}" cron list --all --json | jq -e \
    --arg name "$name" --arg expr "$cron_expr" --arg tz "$tz" \
    --argjson timeout "$PROMO_CRON_TIMEOUT_SECONDS" \
    '.jobs[] | select(
      .name == $name and
      .enabled == true and
      .schedule.expr == $expr and
      .schedule.tz == $tz and
      .payload.timeoutSeconds == $timeout
    )' >/dev/null; then
    printf 'ERROR: %s cron did not land as "%s" %s with a %ss timeout. Current state:\n' \
      "$name" "$cron_expr" "$tz" "$PROMO_CRON_TIMEOUT_SECONDS" >&2
    "${openclaw[@]}" cron list --all --json | jq -r --arg name "$name" \
      '.jobs[] | select(.name == $name) |
       "  schedule=\(.schedule.expr) tz=\(.schedule.tz) timeout=\(.payload.timeoutSeconds) enabled=\(.enabled)"' >&2
    return 1
  fi

  printf '%s cron registered (%s, %s %s, timeout %ss).\n' \
    "$name" "$existing_id" "$cron_expr" "$tz" "$PROMO_CRON_TIMEOUT_SECONDS"
}
