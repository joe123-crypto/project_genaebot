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
for private_dir in "$OC_DIR/credentials" "$OC_DIR/identity" "$OC_DIR/devices" "$OC_DIR/agents"; do
  if [ -d "$private_dir" ]; then
    chmod 0700 "$private_dir"
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
  "$OC_DIR/agent-workspace-admin" \
  "$OC_DIR/agent-workspace-public" \
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

# Secondary agents do not inherit model-provider environment keys at run time.
# Materialize the existing gateway key as a static profile for the restricted
# public agent before the gateway opens and locks the auth store.
if [ -n "${AI_GATEWAY_API_KEY:-}" ] && grep -q '"id"[[:space:]]*:[[:space:]]*"public"' "$OC_DIR/openclaw.json"; then
  runuser -u joseph -- env PUBLIC_MODEL_API_KEY="$AI_GATEWAY_API_KEY" python3 - <<'PY'
import json
import os
import sqlite3
import time
from pathlib import Path

agent_dir = Path("/home/joseph/.openclaw/agents/public/agent")
agent_dir.mkdir(parents=True, exist_ok=True)
database_path = agent_dir / "openclaw-agent.sqlite"
database = sqlite3.connect(database_path)
database.execute("UPDATE schema_meta SET agent_id = 'public' WHERE role = 'agent'")
database.execute(
    "CREATE TABLE IF NOT EXISTS auth_profile_store ("
    "store_key TEXT PRIMARY KEY, store_json TEXT NOT NULL, updated_at INTEGER NOT NULL)"
)
row = database.execute(
    "SELECT store_json FROM auth_profile_store WHERE store_key = 'primary'"
).fetchone()
try:
    payload = json.loads(row[0]) if row else {"version": 1, "profiles": {}}
except (TypeError, json.JSONDecodeError):
    payload = {"version": 1, "profiles": {}}
payload.setdefault("version", 1)
profiles = payload.setdefault("profiles", {})
profiles["vercel-ai-gateway:public-env"] = {
    "type": "api_key",
    "provider": "vercel-ai-gateway",
    "key": os.environ["PUBLIC_MODEL_API_KEY"],
}
database.execute(
    "INSERT INTO auth_profile_store(store_key, store_json, updated_at) "
    "VALUES ('primary', ?, ?) "
    "ON CONFLICT(store_key) DO UPDATE SET store_json=excluded.store_json, updated_at=excluded.updated_at",
    (json.dumps(payload, separators=(",", ":")), int(time.time() * 1000)),
)
database.commit()
database.close()
os.chmod(database_path, 0o600)
PY
  log "provisioned public agent model authentication"
fi

# The currently published @openclaw/whatsapp 2026.6.11 imports a channel SDK
# helper that the published openclaw 2026.6.11 package does not export. Apply a
# narrow idempotent compatibility shim until the registry packages converge.
python3 - <<'PY'
from pathlib import Path

root = Path("/home/joseph/.openclaw/npm")
marker = "function formatInboundMediaUnavailableText({ body, mediaPlaceholder, notice })"
implementation = '''function formatInboundMediaUnavailableText({ body, mediaPlaceholder, notice }) {
\treturn [body, mediaPlaceholder, notice].filter((value) => typeof value === "string" && value.trim()).join("\\n");
}
'''
patched = 0
for path in root.glob("**/@openclaw/whatsapp/dist/monitor-*.js"):
    text = path.read_text(encoding="utf-8")
    if marker in text:
        continue
    if "formatInboundMediaUnavailableText" not in text:
        continue
    updated = text.replace(", formatInboundMediaUnavailableText,", ",", 1)
    anchor = "//#region"
    if updated == text or anchor not in updated:
        raise SystemExit(f"unexpected WhatsApp compatibility target: {path}")
    updated = updated.replace(anchor, implementation + anchor, 1)
    path.write_text(updated, encoding="utf-8")
    patched += 1
if patched:
    print(f"[entrypoint] patched {patched} WhatsApp runtime file(s) for SDK compatibility")
PY

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
