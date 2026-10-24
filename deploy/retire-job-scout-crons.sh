#!/usr/bin/env bash
set -euo pipefail

# One-off cleanup after Job Scout was retired: removes the crons whose commands
# no longer exist in the workspace, so they stop failing every day. Idempotent:
# a cron that is already gone is skipped. The two job feeds are NOT removed here;
# deploy/install-vacancy-lamp-feed.sh and deploy/install-hustlers-feed.sh adopt
# and rename their old job-scout-* crons in place.
#
# Run with --dry-run first to see what would be removed.

# shellcheck disable=SC1091  # sourced at runtime from the same deploy/ directory
source "$(dirname "${BASH_SOURCE[0]}")/gateway-exec.sh"

RETIRED_NAMES=(
  job-scout
  job-scout-pilot
  job-scout-approvals
  job-scout-cv-conversion
  job-scout-channel-health
  algeria-exhibitions
  github-issue-tracker
)

dry_run=0
case "${1:-}" in
  "") ;;
  --dry-run) dry_run=1 ;;
  *)
    printf 'usage: %s [--dry-run]\n' "$0" >&2
    exit 2
    ;;
esac

openclaw=("${GATEWAY_OPENCLAW[@]}")
jobs_json="$("${openclaw[@]}" cron list --all --json)"

removed=0
for name in "${RETIRED_NAMES[@]}"; do
  ids="$(printf '%s' "$jobs_json" | jq -r --arg name "$name" '.jobs[] | select(.name == $name) | .id')"
  [ -n "$ids" ] || continue
  while IFS= read -r id; do
    [ -n "$id" ] || continue
    if [ "$dry_run" = 1 ]; then
      printf 'would remove %s (%s)\n' "$name" "$id"
    else
      "${openclaw[@]}" cron remove "$id" --json >/dev/null
      printf 'removed %s (%s)\n' "$name" "$id"
    fi
    removed=$((removed + 1))
  done <<< "$ids"
done

if [ "$removed" = 0 ]; then
  printf 'no retired crons found\n'
fi
