#!/usr/bin/env bash
set -euo pipefail

runner_dir="${OPENCLAW_RUNNER_DIR:-/home/joseph/actions-runner-openclaw-prod}"
source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
override_src="$source_root/deploy/systemd/actions-runner-openclaw-prod.override.conf"
restart_runner=0

if [ "${1:-}" = --restart ]; then
  restart_runner=1
elif [ "$#" -ne 0 ]; then
  printf 'usage: %s [--restart]\n' "$0" >&2
  exit 2
fi

[ "$(id -u)" -eq 0 ] || { printf 'ERROR: run as root\n' >&2; exit 1; }
[ -f "$runner_dir/.service" ] || { printf 'ERROR: missing %s/.service\n' "$runner_dir" >&2; exit 1; }
[ -f "$override_src" ] || { printf 'ERROR: missing runner override: %s\n' "$override_src" >&2; exit 1; }

service_name="$(tr -d '\r\n' <"$runner_dir/.service")"
case "$service_name" in
  actions.runner.*.service) ;;
  *) printf 'ERROR: unexpected runner service name: %s\n' "$service_name" >&2; exit 1 ;;
esac

override_dst="/etc/systemd/system/$service_name.d/override.conf"
install -D -m 0644 "$override_src" "$override_dst"
systemctl daemon-reload
systemctl reset-failed "$service_name" 2>/dev/null || true
systemctl enable "$service_name"
if [ "$restart_runner" = 1 ]; then
  systemctl restart "$service_name"
fi
systemctl is-enabled "$service_name"
printf 'Installed runner recovery override for %s\n' "$service_name"
