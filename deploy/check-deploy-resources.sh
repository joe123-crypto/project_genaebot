#!/usr/bin/env bash
set -euo pipefail

MEMINFO_PATH="${OPENCLAW_DEPLOY_MEMINFO_PATH:-/proc/meminfo}"
MIN_AVAILABLE_MIB="${OPENCLAW_DEPLOY_MIN_AVAILABLE_MIB:-1024}"
MIN_HEADROOM_MIB="${OPENCLAW_DEPLOY_MIN_HEADROOM_MIB:-6144}"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

case "$MIN_AVAILABLE_MIB" in ''|*[!0-9]*) die "minimum available memory must be a whole MiB value" ;; esac
case "$MIN_HEADROOM_MIB" in ''|*[!0-9]*) die "minimum headroom must be a whole MiB value" ;; esac

[ -r "$MEMINFO_PATH" ] || die "cannot read memory information from $MEMINFO_PATH"

read_meminfo_kib() {
  local key="$1"
  awk -v key="$key" '$1 == key ":" { print $2; found=1; exit } END { if (!found) exit 1 }' "$MEMINFO_PATH"
}

mem_available_kib="$(read_meminfo_kib MemAvailable)" || die "MemAvailable is missing from $MEMINFO_PATH"
swap_free_kib="$(read_meminfo_kib SwapFree)" || die "SwapFree is missing from $MEMINFO_PATH"
swap_total_kib="$(read_meminfo_kib SwapTotal)" || die "SwapTotal is missing from $MEMINFO_PATH"

min_available_kib=$((MIN_AVAILABLE_MIB * 1024))
min_headroom_kib=$((MIN_HEADROOM_MIB * 1024))
headroom_kib=$((mem_available_kib + swap_free_kib))

printf 'Deployment memory preflight: MemAvailable=%s MiB SwapFree=%s MiB SwapTotal=%s MiB CombinedHeadroom=%s MiB\n' \
  "$((mem_available_kib / 1024))" \
  "$((swap_free_kib / 1024))" \
  "$((swap_total_kib / 1024))" \
  "$((headroom_kib / 1024))"

if [ "$mem_available_kib" -ge "$min_available_kib" ] && \
   [ "$headroom_kib" -ge "$min_headroom_kib" ]; then
  printf '[OK] deployment memory headroom is sufficient\n'
  exit 0
fi

printf 'ERROR: refusing deployment before production mutation; require MemAvailable >= %s MiB and combined headroom >= %s MiB\n' \
  "$MIN_AVAILABLE_MIB" "$MIN_HEADROOM_MIB" >&2
if [ "$MEMINFO_PATH" = /proc/meminfo ] && command -v ps >/dev/null 2>&1; then
  printf 'Largest resident-memory consumers:\n' >&2
  ps -eo pid,user,rss,comm,args --sort=-rss 2>/dev/null | head -11 >&2 || true
fi
exit 1
