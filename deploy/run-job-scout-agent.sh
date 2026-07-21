#!/usr/bin/env bash
set -euo pipefail

CONTAINER="${OPENCLAW_CONTAINER:-openclaw-gateway}"
DOCKER="${DOCKER:-docker}"
exec "$DOCKER" exec --user 1000:1000 "$CONTAINER" \
  /home/joseph/.openclaw/workspace/bin/job-scout-agent "$@"
