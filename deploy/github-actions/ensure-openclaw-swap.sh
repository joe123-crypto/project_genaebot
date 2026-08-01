#!/usr/bin/env bash
set -euo pipefail

swap_file="${OPENCLAW_SWAP_FILE:-/swap-openclaw.img}"
swap_size_gib="${OPENCLAW_SWAP_SIZE_GIB:-8}"
fstab_path="${OPENCLAW_FSTAB_PATH:-/etc/fstab}"

[ "$(id -u)" -eq 0 ] || { printf 'ERROR: run as root\n' >&2; exit 1; }
[ "$swap_file" = /swap-openclaw.img ] || { printf 'ERROR: refusing unexpected swap target: %s\n' "$swap_file" >&2; exit 1; }
case "$swap_size_gib" in
  ''|*[!0-9]*) printf 'ERROR: swap size must be a whole GiB value\n' >&2; exit 1 ;;
esac
[ "$swap_size_gib" -ge 1 ] || { printf 'ERROR: swap size must be positive\n' >&2; exit 1; }
[ -f "$fstab_path" ] || { printf 'ERROR: fstab path is not a regular file: %s\n' "$fstab_path" >&2; exit 1; }

expected_bytes=$((swap_size_gib * 1024 * 1024 * 1024))
if [ -L "$swap_file" ]; then
  printf 'ERROR: refusing symbolic-link swap target: %s\n' "$swap_file" >&2
  exit 1
fi

if [ -e "$swap_file" ]; then
  [ -f "$swap_file" ] || { printf 'ERROR: swap target is not a regular file: %s\n' "$swap_file" >&2; exit 1; }
  actual_bytes="$(stat -c %s "$swap_file")"
  [ "$actual_bytes" -eq "$expected_bytes" ] || {
    printf 'ERROR: existing swap target has unexpected size: %s bytes\n' "$actual_bytes" >&2
    exit 1
  }
else
  available_bytes="$(df -PB1 / | awk 'NR == 2 {print $4}')"
  [ "$available_bytes" -gt $((expected_bytes + 2 * 1024 * 1024 * 1024)) ] || {
    printf 'ERROR: insufficient disk headroom for %s GiB swap file\n' "$swap_size_gib" >&2
    exit 1
  }
  fallocate -l "${swap_size_gib}G" "$swap_file"
  chmod 0600 "$swap_file"
  mkswap "$swap_file"
fi

chmod 0600 "$swap_file"
if ! swapon --show=NAME --noheadings | awk '{$1=$1; print}' | grep -Fqx "$swap_file"; then
  swapon "$swap_file"
fi

fstab_line="$swap_file none swap sw 0 0"
if ! grep -Fqx "$fstab_line" "$fstab_path"; then
  printf '%s\n' "$fstab_line" >>"$fstab_path"
fi

swapon --show
printf 'Persistent OpenClaw deployment swap is ready at %s (%s GiB).\n' "$swap_file" "$swap_size_gib"
