#!/usr/bin/env bash
# Reconcile the seven WhatsApp promo crons on the live gateway with what this repo
# declares in deploy/promo-crons.json.
#
# Cron state lives on the gateway's Railway volume, so merging a schedule or timeout
# change ships nothing on its own -- this is the one command that applies it, and
# --check is the one command that says whether it needs applying.
#
# This script performs no gateway writes itself. Every change goes through
# deploy/install-<promo>-promo.sh, which sources install-promo.sh and calls
# register_promo_cron, so running this and running a wrapper by hand are the same code
# path and each promo still gets register_promo_cron's own read-back verification.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="$ROOT/deploy/promo-crons.json"
DRIFT="$ROOT/deploy/promo-cron-drift.py"

usage() {
  cat <<'USAGE'
usage: deploy/reinstall-promo-crons.sh [--check] [--only PROMO]... [--dry-run]
                                       [--json] [--allow-overrides] [-h|--help]

  (no flags)          re-register every promo, then re-check
  --check             read-only: compare live gateway cron state to the manifest
  --only PROMO        limit to this promo (slug or job name); repeatable
  --dry-run           print the resolved plan; contact no gateway at all
  --json              machine-readable report (implies --check)
  --allow-overrides   proceed despite environment overrides that disagree

exit: 0 in sync / all applied, 1 drift or an installer failed, 2 usage or bad manifest,
      75 gateway unreachable (nothing inspected, nothing changed)
USAGE
}

mode=apply
dry=0
as_json=0
allow_overrides=0
only=()

while [ $# -gt 0 ]; do
  case "$1" in
    --check) mode=check ;;
    --dry-run) dry=1 ;;
    --json) as_json=1; mode=check ;;
    --allow-overrides) allow_overrides=1 ;;
    --only)
      [ $# -ge 2 ] || { printf 'ERROR: --only needs a value\n' >&2; exit 2; }
      only+=("$2"); shift ;;
    --only=*) only+=("${1#--only=}") ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'ERROR: unknown argument: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

for dep in jq python3; do
  command -v "$dep" >/dev/null 2>&1 || {
    printf 'ERROR: %s is required on this machine (not on the gateway).\n' "$dep" >&2
    exit 2
  }
done
[ -f "$MANIFEST" ] || { printf 'ERROR: missing %s\n' "$MANIFEST" >&2; exit 2; }
jq -e . "$MANIFEST" >/dev/null 2>&1 || { printf 'ERROR: %s is not valid JSON\n' "$MANIFEST" >&2; exit 2; }

declared_timeout="$(jq -r '.timeoutSeconds' "$MANIFEST")"

# slug \t name \t installer \t envPrefix \t cron \t tz \t failureAlertTo, in manifest
# order -- which is schedule order, the order an operator thinks about the day in.
all_rows=()
while IFS= read -r row; do all_rows+=("$row"); done < <(
  jq -r '.promos[] | [.slug,.name,.installer,.envPrefix,.cron,.tz,.failureAlertTo] | @tsv' "$MANIFEST"
)

rows=()
if [ ${#only[@]} -eq 0 ]; then
  rows=("${all_rows[@]}")
else
  for want in "${only[@]}"; do
    matched=0
    for row in "${all_rows[@]}"; do
      IFS=$'\t' read -r slug name _ _ _ _ _ <<<"$row"
      if [ "$want" = "$slug" ] || [ "$want" = "$name" ]; then rows+=("$row"); matched=1; fi
    done
    if [ "$matched" -eq 0 ]; then
      printf 'ERROR: unknown promo: %s\nknown: %s\n' "$want" \
        "$(jq -r '[.promos[].slug] | join(", ")' "$MANIFEST")" >&2
      exit 2
    fi
  done
fi

# An operator with a stale PROMO_CRON_TIMEOUT_SECONDS=600 exported would otherwise get a
# clean "7 applied" followed immediately by a seven-way drift report.
disagreements=()
if [ -n "${PROMO_CRON_TIMEOUT_SECONDS:-}" ] && \
   [ "$PROMO_CRON_TIMEOUT_SECONDS" != "$declared_timeout" ]; then
  disagreements+=("PROMO_CRON_TIMEOUT_SECONDS=$PROMO_CRON_TIMEOUT_SECONDS (manifest: $declared_timeout)")
fi
for row in "${rows[@]}"; do
  IFS=$'\t' read -r _ _ _ prefix cron tz alert <<<"$row"
  for pair in "CRON_EXPR:$cron" "TZ:$tz" "FAILURE_ALERT_TO:$alert"; do
    suffix="${pair%%:*}"; want="${pair#*:}"
    var="${prefix}_${suffix}"
    have="${!var:-}"
    if [ -n "$have" ] && [ "$have" != "$want" ]; then
      disagreements+=("$var=$have (manifest: $want)")
    fi
  done
done
if [ ${#disagreements[@]} -gt 0 ]; then
  printf 'ERROR: environment overrides disagree with %s:\n' "$MANIFEST" >&2
  printf '  %s\n' "${disagreements[@]}" >&2
  if [ "$allow_overrides" -eq 0 ]; then
    printf 'Unset them, or pass --allow-overrides to proceed anyway.\n' >&2
    exit 2
  fi
  printf 'Proceeding anyway (--allow-overrides).\n' >&2
fi

# shellcheck disable=SC1091  # sourced at runtime from the same deploy/ directory
source "$ROOT/deploy/gateway-exec.sh"

if [ "$dry" -eq 1 ]; then
  printf 'gateway mechanism: %s\n' "$GATEWAY_EXEC_MODE"
  printf 'manifest: %s (timeout %ss)\n\n' "$MANIFEST" "$declared_timeout"
  for row in "${rows[@]}"; do
    IFS=$'\t' read -r _ name installer _ cron tz _ <<<"$row"
    printf '%-26s %-12s %-14s via %s\n' "$name" "$cron" "$tz" "$installer"
  done
  printf '\n%d promo(s) planned; no gateway was contacted.\n' "${#rows[@]}"
  exit 0
fi

live_state="$(mktemp)"
read_error="$(mktemp)"
retired_state="$(mktemp)"
trap 'rm -f "$live_state" "$read_error" "$retired_state"' EXIT

read_live() {
  if ! "${GATEWAY_OPENCLAW[@]}" cron list --all --json >"$live_state" 2>"$read_error"; then
    return 1
  fi
}

# One read up front: a full apply is ~28 round trips over `railway ssh`, so if the
# gateway is unreachable, find out before running a single installer.
if ! read_live; then
  printf 'ERROR: could not read the gateway'"'"'s cron state (%s mode).\n' "$GATEWAY_EXEC_MODE" >&2
  sed 's/^/  /' "$read_error" >&2 || true
  printf 'Nothing was inspected and nothing was changed.\n' >&2
  exit 75
fi

if ! "${GATEWAY_EXEC[@]}" python3 /home/joseph/.openclaw/workspace/bin/console-campaign-check --deleted >"$retired_state"; then
  printf 'ERROR: could not verify campaign retirements; no cron was installed.\n' >&2
  exit 75
fi
drift_args=(--manifest "$MANIFEST" --live "$live_state" --deleted "$retired_state")
for want in ${only[@]+"${only[@]}"}; do drift_args+=(--only "$want"); done
[ "$as_json" -eq 1 ] && drift_args+=(--json)

if [ "$mode" = check ]; then
  set +e
  python3 "$DRIFT" "${drift_args[@]}"
  rc=$?
  set -e
  exit "$rc"
fi

failed=()
applied=0
for row in "${rows[@]}"; do
  IFS=$'\t' read -r _ name installer _ _ _ _ <<<"$row"
  printf '\n==> %s (%s)\n' "$name" "$installer"
  # Keep going on failure: aborting partway leaves the rest on their old schedule,
  # which is exactly the state this tool exists to end.
  if bash "$ROOT/$installer"; then
    applied=$((applied + 1))
  else
    failed+=("$name")
  fi
done

printf '\n%d of %d installer(s) succeeded.\n' "$applied" "${#rows[@]}"
if [ ${#failed[@]} -gt 0 ]; then
  printf 'failed: %s\n' "${failed[*]}" >&2
fi

printf '\nre-checking against the gateway...\n'
if ! read_live; then
  printf 'ERROR: could not re-read the gateway after applying.\n' >&2
  exit 75
fi
set +e
python3 "$DRIFT" "${drift_args[@]}"
rc=$?
set -e
if [ ${#failed[@]} -gt 0 ]; then exit 1; fi
exit "$rc"
