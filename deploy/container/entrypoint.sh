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
  OPENROUTER_API_KEY
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
# Materialize the OpenRouter key as a static profile for the restricted public
# agent before the gateway opens and locks the auth store.
# OpenClaw >=2026.9 keys agents under agents.entries; older configs used agents.list.
if [ -n "${OPENROUTER_API_KEY:-}" ] && python3 -c '
import json, sys
agents = json.load(open(sys.argv[1])).get("agents", {})
sys.exit(0 if "public" in (agents.get("entries") or {}) or any(a.get("id") == "public" for a in agents.get("list", [])) else 1)
' "$OC_DIR/openclaw.json" 2>/dev/null; then
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

# If a command was passed (e.g. `docker compose run --rm openclaw openclaw channels
# login --channel whatsapp`), run it as joseph after the setup above instead of the
# gateway. The normal `docker compose up` path passes no command, so it falls through
# to the gateway. This lets one-off maintenance commands (channels login, doctor, …)
# run with the same env/ownership prep without starting a second competing gateway.
if [ "$#" -gt 0 ]; then
  log "running one-off command as joseph: $*"
  exec runuser -u joseph -- "$@"
fi

# OpenClaw upgrade migration. A newer OpenClaw refuses to start on state/config
# written by an older release (gateway.maintenance_required) until
# `openclaw doctor --fix` has run; doctor may need a second pass once it has
# upgraded the external plugins. Doctor also "repairs" dmPolicy=open by adding
# "*" to the WhatsApp allowlists, which would widen who can reach the bot, so
# those lists are restored from the pre-upgrade snapshot. Runs when the
# installed version has no completion marker or differs from the one that last
# wrote openclaw.json; an interrupted run resumes from its original snapshot.
openclaw_installed="$(runuser -u joseph -- openclaw --version 2>/dev/null | grep -oE '[0-9]{4}\.[0-9]+\.[0-9]+' | head -n 1 || true)"
openclaw_recorded="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("meta", {}).get("lastTouchedVersion", ""))' "$OC_DIR/openclaw.json" 2>/dev/null || true)"
upgrade_marker="$OC_DIR/.openclaw-upgrade-$openclaw_installed"
if [ -n "$openclaw_installed" ] && { [ ! -e "$upgrade_marker.done" ] || [ "$openclaw_installed" != "$openclaw_recorded" ]; }; then
  upgrade_snapshot="$OC_DIR/openclaw.json.pre-upgrade-$openclaw_installed"
  if [ ! -e "$upgrade_marker.started" ] || [ ! -e "$upgrade_snapshot" ]; then
    cp -p "$OC_DIR/openclaw.json" "$upgrade_snapshot"
    runuser -u joseph -- touch "$upgrade_marker.started"
  fi
  log "OpenClaw ${openclaw_recorded:-unknown} -> $openclaw_installed: running doctor --fix (config snapshot: $upgrade_snapshot)"
  for doctor_pass in 1 2 3; do
    if runuser -u joseph -- openclaw doctor --fix --non-interactive --no-workspace-suggestions; then
      break
    fi
    if [ "$doctor_pass" = 3 ]; then
      echo "[entrypoint] FATAL: openclaw doctor --fix did not converge; see output above." >&2
      exit 1
    fi
    log "doctor pass $doctor_pass did not finish cleanly; retrying"
  done
  runuser -u joseph -- python3 - "$upgrade_snapshot" "$OC_DIR/openclaw.json" <<'PY'
import json
import os
import sys
import tempfile

ACCESS_PATHS = [
    ["channels", "whatsapp", "allowFrom"],
    ["channels", "whatsapp", "groupAllowFrom"],
    ["tools", "elevated", "allowFrom"],
]
with open(sys.argv[1], encoding="utf-8") as fh:
    before = json.load(fh)
with open(sys.argv[2], encoding="utf-8") as fh:
    after = json.load(fh)
for path in ACCESS_PATHS:
    src, dst = before, after
    for key in path[:-1]:
        src = src.get(key) if isinstance(src, dict) else None
        dst = dst.get(key) if isinstance(dst, dict) else None
    if not isinstance(dst, dict):
        continue
    if isinstance(src, dict) and path[-1] in src:
        dst[path[-1]] = src[path[-1]]
    else:
        dst.pop(path[-1], None)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(sys.argv[2]))
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(after, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
os.chmod(tmp, 0o600)
os.replace(tmp, sys.argv[2])
PY
  runuser -u joseph -- openclaw config validate
  runuser -u joseph -- touch "$upgrade_marker.done"
  rm -f "$upgrade_marker.started"
  log "OpenClaw upgrade migration to $openclaw_installed complete"
fi

# --bind lan  : listen on all container interfaces so the published port is reachable
#               (locally Docker only publishes it to 127.0.0.1; see compose).
# --tailscale off : no tailscaled in the container; remote access is via platform ingress.
log "starting openclaw gateway on port $PORT as joseph (bind=lan, tailscale=off)"
exec runuser -u joseph -- openclaw gateway --port "$PORT" --bind lan --tailscale off
