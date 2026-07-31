#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="${OPENCLAW_DEPLOY_DIR:-/home/joseph/openclaw-deploy}"
DATA_DIR="${JOB_SCOUT_CHANNEL_DATA_DIR:-/home/joseph/projects/genaebot/runtime/job-scout-channel}"
DOCKER="${DOCKER:-docker}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

mkdir -p "$DATA_DIR/auth" "$DATA_DIR/media"
chmod 0700 "$DATA_DIR" "$DATA_DIR/auth" "$DATA_DIR/media"
cd "$DEPLOY_DIR"
"$DOCKER" compose --profile job-scout-channel stop job-scout-channel-listener >/dev/null 2>&1 || true
"$DOCKER" compose --profile job-scout-channel build job-scout-channel-listener
printf 'Scan the QR code with the WhatsApp account that should follow the configured Job Scout channels.\n'
"$DOCKER" compose --profile job-scout-channel run --rm --no-deps \
  -e JOB_SCOUT_CHANNEL_PAIRING=1 job-scout-channel-listener

if [ ! -s "$DATA_DIR/auth/creds.json" ]; then
  printf 'ERROR: pairing did not create WhatsApp credentials.\n' >&2
  exit 1
fi
"$DOCKER" compose --profile job-scout-channel up -d --build --force-recreate job-scout-channel-listener
for _attempt in $(seq 1 24); do
  id="$("$DOCKER" compose --profile job-scout-channel ps -q job-scout-channel-listener)"
  health="$("$DOCKER" inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$id" 2>/dev/null || true)"
  if [ "$health" = healthy ]; then
    bash "$ROOT/deploy/install-job-scout-channel-health.sh"
    printf 'Job Scout channel listener is paired and healthy.\n'
    exit 0
  fi
  if [ "$health" = unhealthy ]; then
    break
  fi
  sleep 5
done
printf 'ERROR: paired listener did not become healthy (health=%s).\n' "${health:-missing}" >&2
"$DOCKER" compose --profile job-scout-channel logs --tail 80 job-scout-channel-listener >&2 || true
exit 1
