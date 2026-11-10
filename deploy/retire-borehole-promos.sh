#!/usr/bin/env bash
# Idempotent migration. The installer calls this after verifying the retained job.
set -euo pipefail
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/gateway-exec.sh"
dry=0
case "${1:-}" in
  "") ;;
  --dry-run) dry=1 ;;
  *) printf 'usage: %s [--dry-run]\n' "$0" >&2; exit 2 ;;
esac
jobs_json="$("${GATEWAY_OPENCLAW[@]}" cron list --all --json)"
if ! jq -e '.jobs | type == "array"' <<<"$jobs_json" >/dev/null; then
  printf 'ERROR: unusable live cron state.\n' >&2; exit 1
fi
if jq -e '.jobs[] | select((.name == "borehole-paid-promo" or .name == "big-player-promo" or .name == "borehole-drilling-promo") and .state.runningAtMs != null)' <<<"$jobs_json" >/dev/null; then
  printf 'ERROR: a borehole campaign is running; let it finish before migrating.\n' >&2; exit 1
fi
expected_cron="15 7 * * *"
expected_tz="Africa/Harare"
if [ "$dry" -eq 0 ]; then
  saved_schedule="$("${GATEWAY_EXEC[@]}" python3 /home/joseph/.openclaw/workspace/bin/console-campaign-check --schedule borehole-drilling)"
  if [ "$saved_schedule" != '{}' ]; then
    expected_cron="$(jq -er '.cron' <<<"$saved_schedule")"
    expected_tz="$(jq -er '.timezone' <<<"$saved_schedule")"
  fi
fi
if [ "$dry" -eq 0 ] && ! jq -e --arg cron "$expected_cron" --arg tz "$expected_tz" '.jobs[] | select(.name == "borehole-drilling-promo" and .enabled == true and .schedule.expr == $cron and .schedule.tz == $tz and .payload.argv == ["python3", "/home/joseph/.openclaw/workspace/cron/borehole-drilling-promo/post_ad.py"])' <<<"$jobs_json" >/dev/null; then
  printf 'ERROR: verify the replacement borehole job before retiring old jobs.\n' >&2; exit 1
fi
while IFS=$'\t' read -r id name; do
  [ -n "$id" ] || continue
  if [ "$dry" -eq 1 ]; then
    printf 'would remove %s (%s)\n' "$name" "$id"
  else
    "${GATEWAY_OPENCLAW[@]}" cron remove "$id" --json >/dev/null
    printf 'removed %s (%s)\n' "$name" "$id"
  fi
done < <(jq -r '.jobs[] | select(.name == "borehole-paid-promo" or .name == "big-player-promo") | [.id, .name] | @tsv' <<<"$jobs_json")
if [ "$dry" -eq 0 ]; then
  "${GATEWAY_OPENCLAW[@]}" cron list --all --json | jq -e '[.jobs[] | select(.name == "borehole-paid-promo" or .name == "big-player-promo")] | length == 0' >/dev/null
fi
