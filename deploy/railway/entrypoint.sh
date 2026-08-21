#!/usr/bin/env bash
# OpenClaw gateway container entrypoint — Railway (gateway-only) variant.
# Runs as root (under tini), prepares the mounted state volume, applies the baked
# source overlay, then drops to `joseph`.
#
# Differences vs. ../entrypoint.sh:
#   * Adds a source-overlay sync (step 1.5) that reproduces the Compose
#     bind-mounts (live-source/workspace + agent-workspace-*/*.md) from the image
#     baked at /opt/live-source, because Railway has no host bind-mounts.
#   * Drops the Webetu worker-config warning (Webetu is dormant here).
set -euo pipefail

# IMPORTANT: openclaw treats OPENCLAW_HOME as the BASE dir and appends "/.openclaw".
# It must NOT point at the .openclaw dir itself, or config resolves to
# .openclaw/.openclaw/openclaw.json (missing). Unset it and rely on HOME=/home/joseph.
unset OPENCLAW_HOME
OC_DIR="/home/joseph/.openclaw"
LIVE_SOURCE="/opt/live-source"
PORT="${OPENCLAW_GATEWAY_PORT:-18789}"
ENV_FILE="$OC_DIR/.env"
export OPENCLAW_TRAJECTORY="${OPENCLAW_TRAJECTORY:-0}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"

log() { echo "[entrypoint] $*"; }

# 0. Seed mode: keep the container alive with an empty/unseeded volume so state
#    can be pushed in via `railway ssh`. Skips ALL setup and the openclaw.json
#    guard below. Set OPENCLAW_SEED_MODE=1 for the one-time seed deploy, push the
#    state, then unset it and redeploy to boot the real gateway.
if [ "${OPENCLAW_SEED_MODE:-0}" = "1" ]; then
  log "SEED MODE: idling for state seeding (no gateway, no openclaw.json guard)"
  exec sleep infinity
fi

# 0.5 Auto-seed: if durable state is absent but seed tarballs were uploaded to the
#     volume root (via `railway volume files upload`), extract them once and
#     remove the tarballs. openclaw-state.tgz was produced with `tar -C <vol> .`
#     so it unpacks directly under $OC_DIR; openclaw-npm.tgz unpacks under npm/.
if [ ! -f "$OC_DIR/openclaw.json" ] && [ -f "$OC_DIR/openclaw-state.tgz" ]; then
  log "auto-seed: extracting openclaw-state.tgz into $OC_DIR"
  tar -xzf "$OC_DIR/openclaw-state.tgz" -C "$OC_DIR"
  if [ -f "$OC_DIR/openclaw-npm.tgz" ]; then
    log "auto-seed: extracting openclaw-npm.tgz into $OC_DIR/npm"
    mkdir -p "$OC_DIR/npm"
    tar -xzf "$OC_DIR/openclaw-npm.tgz" -C "$OC_DIR/npm"
  fi
  rm -f "$OC_DIR/openclaw-state.tgz" "$OC_DIR/openclaw-npm.tgz"
  log "auto-seed: complete"
fi

# 1. The volume must be seeded (see railway/README.md) before first boot.
if [ ! -f "$OC_DIR/openclaw.json" ]; then
  echo "[entrypoint] FATAL: $OC_DIR/openclaw.json not found." >&2
  echo "[entrypoint] Seed the Railway volume first (see railway/README.md)." >&2
  exit 78
fi

# These paths may be volume subdirs that start empty. Ensure OpenClaw sees the
# expected tree even when a cache/temp area is fresh. (On Railway the former
# tmpfs/nested-volume paths are plain dirs on the single mounted volume.)
mkdir -p \
  "$OC_DIR/npm" \
  "$OC_DIR/media/inbound" \
  "$OC_DIR/media/outbound" \
  "$OC_DIR/voice-calls" \
  "$OC_DIR/logs" \
  "$OC_DIR/openclaw-runtime" \
  "$OC_DIR/workspace/tmp" \
  "$OC_DIR/runtime" \
  "$OC_DIR/agent-workspace-admin" \
  "$OC_DIR/agent-workspace-public"

# 1.5 Apply the baked source overlay onto the volume (reproduces the Compose
# bind-mounts, where live-source/workspace is mounted read-only over the whole
# workspace). live-source is the source of truth for workspace/ (incl. skills/)
# and the agent-workspace context markdown; writable runtime state lives under
# runtime/ and agent-workspace-*/, not under workspace/. tmp/ is preserved as
# boot scratch (recreated below); .git/__pycache__ are build cruft.
if [ -d "$LIVE_SOURCE/workspace" ]; then
  rsync -a --delete \
    --exclude 'tmp' \
    --exclude '.git' \
    --exclude '__pycache__' \
    "$LIVE_SOURCE/workspace/" "$OC_DIR/workspace/"
  log "synced source overlay from $LIVE_SOURCE/workspace"
fi
for role in admin public; do
  src="$LIVE_SOURCE/agent-workspace-$role"
  if [ -d "$src" ]; then
    find "$src" -maxdepth 1 -name '*.md' -exec cp -f {} "$OC_DIR/agent-workspace-$role/" \;
  fi
done
# The admin agent context also pulls a few markdown files from workspace/ (see
# docker-compose.override.yml). Mirror those explicitly.
for f in SOUL.md HEARTBEAT.md IDENTITY.md MEMORY.md USER.md; do
  [ -f "$LIVE_SOURCE/workspace/$f" ] && cp -f "$LIVE_SOURCE/workspace/$f" "$OC_DIR/agent-workspace-admin/$f"
done

# 1.6 Runtime reseed: one-shot overlay of live job-scout/gate/account-link state.
#     Upload runtime-seed.tgz (tar of genaebot/runtime) to the volume root via
#     `railway volume files upload`; this extracts it over $OC_DIR/runtime and
#     removes the tarball. Used to replace the stale runtime the volume was
#     seeded with (runtime/ was a host bind-mount, not part of openclaw-state).
if [ -f "$OC_DIR/runtime-seed.tgz" ]; then
  log "runtime reseed: extracting runtime-seed.tgz into $OC_DIR/runtime"
  mkdir -p "$OC_DIR/runtime"
  tar -xzf "$OC_DIR/runtime-seed.tgz" -C "$OC_DIR/runtime"
  rm -f "$OC_DIR/runtime-seed.tgz"
  log "runtime reseed: complete"
fi

# 2. Synthesize .env from environment (Railway service variables). Secrets are
#    never baked into the image; only non-empty vars are written.
SECRET_KEYS=(
  GEMINI_API_KEY
  OPENROUTER_API_KEY
  AI_GATEWAY_API_KEY
  AGENT_GENAI_INTERNAL_API_KEY
  GATEWAY_AUTH_TOKEN
  APP_BASE_URL
  PUBLIC_BASE_URL
  WHATSAPP_ADMIN_ENABLED
  WHATSAPP_RELEVANCE_MODE
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
  # Sanitize: a Railway variable set with surrounding whitespace or quotes would
  # otherwise be written verbatim and rejected downstream (e.g. a quoted
  # AGENT_GENAI_INTERNAL_API_KEY yields HTTP 403 "invalid internal API key",
  # which silently breaks the job-scout group feed and dispatch crons). Trim
  # surrounding whitespace, then one layer of surrounding quotes.
  val="${val#"${val%%[![:space:]]*}"}"
  val="${val%"${val##*[![:space:]]}"}"
  val="${val#\"}"; val="${val%\"}"
  val="${val#\'}"; val="${val%\'}"
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

# 3. Make mutable volume paths owned by joseph (a foreign-uid seed is possible in
# cloud). live-source is baked read-only, so we only chown the volume tree.
for path in \
  "$OC_DIR/.env" \
  "$OC_DIR/openclaw.json" \
  "$OC_DIR/agent-workspace-admin" \
  "$OC_DIR/agent-workspace-public" \
  "$OC_DIR/workspace" \
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
    chown -R joseph:joseph "$path" 2>/dev/null || log "skip ownership fix: $path"
  fi
done

# Secondary agents do not inherit model-provider environment keys at run time.
# Materialize the OpenRouter key as a static profile for the restricted public
# agent before the gateway opens and locks the auth store.
if [ -n "${OPENROUTER_API_KEY:-}" ] && grep -q '"id"[[:space:]]*:[[:space:]]*"public"' "$OC_DIR/openclaw.json"; then
  runuser -u joseph -- env PUBLIC_MODEL_API_KEY="$OPENROUTER_API_KEY" python3 - <<'PY'
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
profiles["openrouter:public-env"] = {
    "type": "api_key",
    "provider": "openrouter",
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
  log "provisioned public agent model authentication (openrouter)"
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

# If a command was passed (e.g. `railway ssh -- openclaw doctor`), run it as
# joseph after the setup above instead of starting the gateway.
if [ "$#" -gt 0 ]; then
  log "running one-off command as joseph: $*"
  exec runuser -u joseph -- "$@"
fi

# --bind lan  : listen on all container interfaces so the platform can reach us.
# --tailscale off : no tailscaled in the container; remote access is via ingress.
log "starting openclaw gateway on port $PORT as joseph (bind=lan, tailscale=off)"
exec runuser -u joseph -- openclaw gateway --port "$PORT" --bind lan --tailscale off
