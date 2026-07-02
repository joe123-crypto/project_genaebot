#!/usr/bin/env bash
set -euo pipefail

docker_bin="${DOCKER_BIN:-/snap/bin/docker}"
attempts="${DOCKER_READY_ATTEMPTS:-90}"
interval="${DOCKER_READY_INTERVAL_SECONDS:-1}"

case "$attempts" in
  ''|*[!0-9]*)
    printf 'DOCKER_READY_ATTEMPTS must be a positive integer\n' >&2
    exit 2
    ;;
esac

if [ "$attempts" -lt 1 ]; then
  printf 'DOCKER_READY_ATTEMPTS must be a positive integer\n' >&2
  exit 2
fi

for _ in $(seq 1 "$attempts"); do
  if "$docker_bin" info >/dev/null 2>&1; then
    exit 0
  fi
  sleep "$interval"
done

printf 'Docker API did not become ready after %s attempts\n' "$attempts" >&2
exit 1
