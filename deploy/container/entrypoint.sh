#!/usr/bin/env bash
# OpenClaw gateway container entrypoint.
# Runs as root (under tini), prepares the mounted volume, then drops to `joseph`.
set -euo pipefail

# IMPORTANT: openclaw treats OPENCLAW_HOME as the BASE dir and appends "/.openclaw".
# It must NOT point at the .openclaw dir itself, or config resolves to
# .openclaw/.openclaw/openclaw.json (missing). Unset it and rely on HOME=/home/joseph.
unset OPENCLAW_HOME
OC_DIR="/home/joseph/.openclaw"
PORT="${OPENCLAW_GATEWAY_PORT:-18789}"
ENV_FILE="$OC_DIR/.env"
WORKER_CONFIG="/etc/webetu-worker/config.json"
export OPENCLAW_TRAJECTORY="${OPENCLAW_TRAJECTORY:-0}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"

log() { echo "[entrypoint] $*"; }

# 1. The volume must be seeded (see deploy/seed-volume.sh) before first boot.
if [ ! -f "$OC_DIR/openclaw.json" ]; then
  echo "[entrypoint] FATAL: $OC_DIR/openclaw.json not found." >&2
  echo "[entrypoint] Seed the volume first: bash deploy/seed-volume.sh" >&2
  exit 78
fi

# These paths may be nested volumes or tmpfs mounts. Ensure OpenClaw sees the
# expected tree even when a cache/temp mount starts empty.
mkdir -p \
  "$OC_DIR/npm" \
  "$OC_DIR/media/inbound" \
  "$OC_DIR/media/outbound" \
  "$OC_DIR/voice-calls" \
  "$OC_DIR/logs" \
  "$OC_DIR/openclaw-runtime" \
  "$OC_DIR/workspace/tmp"

# 2. Synthesize .env from environment (env_file locally / platform secrets in cloud).
#    Secrets are never baked into the image; they arrive as process env and we write
#    the .env the openclaw dotenv provider reads. Only non-empty vars are written.
SECRET_KEYS=(
  GEMINI_API_KEY
  AI_GATEWAY_API_KEY
  AGENT_GENAI_INTERNAL_API_KEY
  GATEWAY_AUTH_TOKEN
  APP_BASE_URL
  PUBLIC_BASE_URL
  WHATSAPP_ADMIN_ENABLED
  JSEARCH_API_KEY
  JSEARCH_RAPIDAPI_KEY
  JSEARCH_RAPIDAPI_HOST
  JSEARCH_PROVIDER
  JSEARCH_DATE_POSTED
  JOB_SCOUT_BROWSER_TOKEN
  JOB_SCOUT_PILOT_USER_IDS
)
wrote_env=0
tmp_env="$(mktemp)"
for key in "${SECRET_KEYS[@]}"; do
  val="${!key:-}"
  if [ -n "$val" ]; then
    printf '%s=%s\n' "$key" "$val" >> "$tmp_env"
    wrote_env=1
  fi
done
if [ "$wrote_env" = "1" ]; then
  install -o joseph -g joseph -m 0600 "$tmp_env" "$ENV_FILE"
  log "wrote $ENV_FILE from environment ($(wc -l < "$ENV_FILE") keys)"
else
  log "no secret env vars set; using existing $ENV_FILE (if present)"
fi
rm -f "$tmp_env"

# 3. Make mutable volumes owned by joseph (host seed may carry foreign uids in
# cloud). Source/context paths can be mounted read-only, so do not recursively
# chown the whole OpenClaw tree.
for path in \
  "$OC_DIR/.env" \
  "$OC_DIR/openclaw.json" \
  "$OC_DIR/agent-workspace" \
  "$OC_DIR/runtime" \
  "$OC_DIR/npm" \
  "$OC_DIR/media" \
  "$OC_DIR/voice-calls" \
  "$OC_DIR/logs" \
  "$OC_DIR/openclaw-runtime" \
  "$OC_DIR/credentials" \
  "$OC_DIR/identity" \
  "$OC_DIR/devices" \
  "$OC_DIR/state" \
  "$OC_DIR/plugin-state" \
  "$OC_DIR/flows" \
  "$OC_DIR/tasks" \
  "$OC_DIR/delivery-queue" \
  "$OC_DIR/memory" \
  "$OC_DIR/agents"; do
  if [ -e "$path" ]; then
    chown -R joseph:joseph "$path" 2>/dev/null || log "skip ownership fix for read-only path: $path"
  fi
done

# 4. Warn if the webetu worker config is still the blank template (worker won't work).
if [ ! -f "$WORKER_CONFIG" ] || ! grep -q '[^[:space:]]' <(sed -n 's/.*firebase_project_id" *: *"\([^"]*\)".*/\1/p' "$WORKER_CONFIG" 2>/dev/null); then
  log "WARNING: $WORKER_CONFIG looks unset — mount the real config to enable Webetu."
fi

# If a command was passed (e.g. `docker compose run --rm openclaw openclaw channels
# login --channel whatsapp`), run it as joseph after the setup above instead of the
# gateway. The normal `docker compose up` path passes no command, so it falls through
# to the gateway. This lets one-off maintenance commands (channels login, doctor, …)
# run with the same env/ownership prep without starting a second competing gateway.
if [ "$#" -gt 0 ]; then
  log "running one-off command as joseph: $*"
  exec runuser -u joseph -- "$@"
fi

# --bind lan  : listen on all container interfaces so the published port is reachable
#               (locally Docker only publishes it to 127.0.0.1; see compose).
# --tailscale off : no tailscaled in the container; remote access is via platform ingress.
log "starting openclaw gateway on port $PORT as joseph (bind=lan, tailscale=off)"
exec runuser -u joseph -- openclaw gateway --port "$PORT" --bind lan --tailscale off
