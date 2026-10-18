#!/usr/bin/env bash
# OpenClaw gateway container entrypoint — Railway (gateway-only) variant.
# Runs as root (under tini), prepares the mounted state volume, applies the baked
# source overlay, then drops to `joseph`.
#
# Differences vs. ../entrypoint.sh:
#   * Adds a source-overlay sync (step 1.5) that reproduces the Compose
#     bind-mounts (live-source/workspace + agent-workspace-*/*.md) from the image
#     baked at /opt/live-source, because Railway has no host bind-mounts.
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
# The admin agent context also pulls a few markdown files from workspace/.
# deploy/ci/check.py asserts this list, so keep the two in step.
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
    # Re-export the cleaned value into the process environment. job-scout's
    # env_default() reads os.environ FIRST and verbatim (config.py), only
    # falling back to .env, so the gateway and the cron subprocesses it spawns
    # would otherwise inherit Railway's raw (quoted) value and keep 403-ing.
    export "$key=$val"
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

# If a command was passed (e.g. `railway ssh -- openclaw doctor`), run it as
# joseph after the setup above instead of starting the gateway.
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
# installed version has no completion marker, differs from the one that last
# wrote openclaw.json, or an npm-installed plugin (e.g. whatsapp) is not at the
# installed version; an interrupted run resumes from its original snapshot.
# Doctor only warns when it cannot update a plugin, and a plugin built for an
# older core fails to register, so the plugin versions are checked explicitly.
# npm runs as joseph and needs a writable cache under $HOME.
install -d -o joseph -g joseph /home/joseph/.npm
chown -R joseph:joseph /home/joseph/.npm
openclaw_installed="$(runuser -u joseph -- openclaw --version 2>/dev/null | grep -oE '[0-9]{4}\.[0-9]+\.[0-9]+' | head -n 1 || true)"
openclaw_recorded="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("meta", {}).get("lastTouchedVersion", ""))' "$OC_DIR/openclaw.json" 2>/dev/null || true)"
stale_openclaw_plugins() {
  runuser -u joseph -- openclaw plugins list --json 2>/dev/null | python3 -c '
import json, sys
try:
    plugins = json.load(sys.stdin).get("plugins", [])
except ValueError:
    plugins = []
print(" ".join(sorted({p["id"] for p in plugins if p.get("origin") == "global" and p.get("version") != sys.argv[1]})))
' "$openclaw_installed" || true
}
upgrade_marker="$OC_DIR/.openclaw-upgrade-$openclaw_installed"
openclaw_stale=""
if [ -n "$openclaw_installed" ] && [ -e "$upgrade_marker.done" ] && [ "$openclaw_installed" = "$openclaw_recorded" ]; then
  openclaw_stale="$(stale_openclaw_plugins)"
fi
if [ -n "$openclaw_installed" ] && { [ ! -e "$upgrade_marker.done" ] || [ "$openclaw_installed" != "$openclaw_recorded" ] || [ -n "$openclaw_stale" ]; }; then
  upgrade_snapshot="$OC_DIR/openclaw.json.pre-upgrade-$openclaw_installed"
  if [ ! -e "$upgrade_marker.started" ] || [ ! -e "$upgrade_snapshot" ]; then
    cp -p "$OC_DIR/openclaw.json" "$upgrade_snapshot"
    runuser -u joseph -- touch "$upgrade_marker.started"
  fi
  log "OpenClaw ${openclaw_recorded:-unknown} -> $openclaw_installed${openclaw_stale:+ (stale plugins: $openclaw_stale)}: running doctor --fix (config snapshot: $upgrade_snapshot)"
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
  openclaw_stale="$(stale_openclaw_plugins)"
  if [ -n "$openclaw_stale" ]; then
    log "updating plugins not at $openclaw_installed: $openclaw_stale"
    # shellcheck disable=SC2086 # plugin ids are space-separated words
    runuser -u joseph -- openclaw plugins update $openclaw_stale || true
    runuser -u joseph -- openclaw doctor --fix --non-interactive --no-workspace-suggestions || true
    openclaw_stale="$(stale_openclaw_plugins)"
  fi
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
  if [ -n "$openclaw_stale" ]; then
    echo "[entrypoint] WARNING: plugins still not at $openclaw_installed: $openclaw_stale. They will fail to load (WhatsApp is unavailable if whatsapp is listed); retrying on the next boot." >&2
  else
    log "OpenClaw upgrade migration to $openclaw_installed complete"
  fi
fi

# Public WhatsApp prompt sync. The public agent's instructions live in the
# volume's openclaw.json (channels.whatsapp.direct["*"].systemPrompt), and nothing
# else on this boot path applies the config template, so copy the tracked prompt
# in on every boot. Only that key is written, after doctor (which could rewrite
# the file) and before the gateway reads its config. Never fatal.
runuser -u joseph -- python3 /usr/local/bin/sync-public-prompt.py \
  "$LIVE_SOURCE/workspace/docs/public-whatsapp-system-prompt.txt" \
  "$OC_DIR/openclaw.json" \
  "$LIVE_SOURCE/deploy/patch-openclaw-config.py" \
  | while IFS= read -r line; do log "$line"; done || true

# A container killed mid-drain (a deploy landing during a promo cron run) leaves
# its gateway owner lease in the state DB for up to its 300 s TTL. The new
# container has a new hostname, so OpenClaw cannot reclaim it early and refuses to
# start; Railway's 10 quick restarts run out in ~2 min and the deploy CRASHES
# (2026-10-18 10:09 UTC). Wait the stale lease out here instead. Never fatal.
runuser -u joseph -- python3 /usr/local/bin/wait-gateway-lease.py \
  "$OC_DIR/state/openclaw.sqlite" \
  | while IFS= read -r line; do log "$line"; done || true

# --bind lan  : listen on all container interfaces so the platform can reach us.
# --tailscale off : no tailscaled in the container; remote access is via ingress.
log "starting openclaw gateway on port $PORT as joseph (bind=lan, tailscale=off)"
exec runuser -u joseph -- openclaw gateway --port "$PORT" --bind lan --tailscale off
