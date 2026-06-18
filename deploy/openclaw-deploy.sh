#!/usr/bin/env bash
set -euo pipefail

OC_DIR="${OPENCLAW_DIR:-/home/joseph/.openclaw}"
DEPLOY_DIR="${OPENCLAW_DEPLOY_DIR:-/home/joseph/openclaw-deploy}"
STAGE_DIR="$DEPLOY_DIR/live-source"
COMPOSE_OVERRIDE_SRC="$OC_DIR/deploy/docker-compose.override.yml"
COMPOSE_OVERRIDE_DST="$DEPLOY_DIR/docker-compose.override.yml"
ENTRYPOINT_SRC="$OC_DIR/deploy/container/entrypoint.sh"
ENTRYPOINT_DST="$DEPLOY_DIR/entrypoint.sh"
SERVICE_NAME="${OPENCLAW_CONTAINER_SERVICE:-openclaw-gateway-container.service}"
CONTAINER="${OPENCLAW_CONTAINER:-openclaw-gateway}"
DOCKER_BIN="${DOCKER:-}"
APPLY_CONTAINER_BOUNDARY=0

log() {
  printf '==> %s\n' "$*"
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

run_systemctl() {
  if systemctl "$@"; then
    return 0
  fi
  if command -v sudo >/dev/null 2>&1 && sudo -n systemctl "$@"; then
    return 0
  fi
  die "systemctl $* failed; rerun with permissions to manage $SERVICE_NAME"
}

require_file() {
  [ -f "$1" ] || die "missing required file: $1"
}

git_cmd() {
  git -c "safe.directory=$OC_DIR" -C "$OC_DIR" "$@"
}

docker_bin() {
  if [ -n "$DOCKER_BIN" ]; then
    printf '%s\n' "$DOCKER_BIN"
  elif command -v /snap/bin/docker >/dev/null 2>&1; then
    printf '%s\n' /snap/bin/docker
  else
    printf '%s\n' docker
  fi
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --apply-container-boundary)
        APPLY_CONTAINER_BOUNDARY=1
        ;;
      -h|--help)
        cat <<'EOF'
Usage: openclaw-deploy.sh [--apply-container-boundary]

By default, deploy validates source, stages curated files, patches the live
container config, restarts the gateway, and runs health checks.

Use --apply-container-boundary to also install the Docker Compose override that
mounts curated source read-only and runtime paths writable. This is intentionally
opt-in because it changes container mounts.
EOF
        exit 0
        ;;
      *)
        die "unknown argument: $1"
        ;;
    esac
    shift
  done
}

check_gitclaw_disabled() {
  if crontab -l 2>/dev/null | grep -Fq '/home/joseph/.openclaw/gitclaw/auto_backup.sh'; then
    die "GitClaw autocommit cron entry is still active"
  fi
}

check_staged_files() {
  if ! git_cmd rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    die "$OC_DIR is not a Git worktree"
  fi

  log "Git status"
  git_cmd status --short

  log "Git diff stat"
  git_cmd diff --stat || true

  local staged
  staged="$(git_cmd diff --cached --name-only || true)"
  if [ -n "$staged" ]; then
    if printf '%s\n' "$staged" | grep -E '(^|/)(\.env|credentials|identity|devices|sessions|memory|logs|state|plugin-state|flows|tasks|media|npm|browser|cache|voice-calls|runtime)(/|$)' >/dev/null; then
      printf '%s\n' "$staged" >&2
      die "refusing deploy with staged runtime/secret files"
    fi
  fi
}

check_docker_access() {
  local docker
  docker="$(docker_bin)"
  if ! command -v "$docker" >/dev/null 2>&1; then
    die "docker command is unavailable: $docker"
  fi
  if ! "$docker" inspect "$CONTAINER" >/dev/null 2>&1; then
    die "cannot inspect container '$CONTAINER'. Rerun with Docker permissions, for example: sudo $OC_DIR/deploy/openclaw-deploy.sh"
  fi
}

syntax_checks() {
  log "Syntax checks"
  node --check "$OC_DIR/workspace/plugins/public-service-gate/index.js"
  node --check "$OC_DIR/workspace/plugins/whatsapp-relevance-gate/index.js"
  local pycache
  pycache="$(mktemp -d)"
  trap 'rm -rf "$pycache"' RETURN
  PYTHONPYCACHEPREFIX="$pycache" python3 -m py_compile \
    "$OC_DIR/workspace/account-link/account_link.py" \
    "$OC_DIR/workspace/account-link/pending_cache.py" \
    "$OC_DIR/workspace/public-news/news_subscriptions.py" \
    "$OC_DIR/workspace/cron/algeria-exhibitions-listener/check_new_events.py" \
    "$OC_DIR/deploy/patch-openclaw-config.py"
  bash -n "$OC_DIR/workspace/bin/openclaw-cap"
  bash -n "$OC_DIR/workspace/cron/github-issue-tracker/check_issue.sh"
  bash -n "$OC_DIR/workspace/cron/webetu-meals/notify-reservations.sh"
  python3 -m json.tool "$OC_DIR/config/openclaw.template.json" >/dev/null
  python3 -m json.tool "$COMPOSE_OVERRIDE_SRC" >/dev/null 2>&1 || true
}

stage_source() {
  require_file "$COMPOSE_OVERRIDE_SRC"
  require_file "$ENTRYPOINT_SRC"
  log "Stage curated source into $STAGE_DIR"
  rm -rf "$STAGE_DIR"
  mkdir -p "$STAGE_DIR/workspace" "$STAGE_DIR/agent-workspace" "$STAGE_DIR/config" "$STAGE_DIR/deploy"

  rsync -a --delete \
    --exclude='**/__pycache__/' \
    --exclude='**/.venv/' \
    --exclude='**/node_modules/' \
    --exclude='**/*.pyc' \
    --exclude='**/*.log' \
    --exclude='**/state.json' \
    --exclude='**/pending-links.json' \
    --exclude='**/subscriptions.json' \
    --include='/AGENTS.md' \
    --include='/HEARTBEAT.md' \
    --include='/IDENTITY.md' \
    --include='/MEMORY.md' \
    --include='/README.md' \
    --include='/SOUL.md' \
    --include='/TOOLS.md' \
    --include='/USER.md' \
    --include='/bin/***' \
    --include='/lib/***' \
    --include='/plugins/***' \
    --include='/docs/***' \
    --include='/account-link/***' \
    --include='/public-news/***' \
    --include='/webetu-preferences/***' \
    --include='/job-scout/*.py' \
    --include='/job-scout/scrapers/***' \
    --include='/cron/***' \
    --include='/skills.lock.json' \
    --include='/skills/genaie-mail/***' \
    --include='/skills/gog/***' \
    --include='/skills/job-scout/SKILL.md' \
    --include='/skills/pdf-edit/SKILL.md' \
    --include='/skills/pdf-edit/pdf_edit.py' \
    --exclude='/**' \
    "$OC_DIR/workspace/" "$STAGE_DIR/workspace/"
  mkdir -p "$STAGE_DIR/workspace/tmp"

  rsync -a --delete "$OC_DIR/deploy/agent-workspace/" "$STAGE_DIR/agent-workspace/"
  install -m 0644 "$OC_DIR/config/openclaw.template.json" "$STAGE_DIR/config/openclaw.template.json"
  install -m 0755 "$OC_DIR/deploy/patch-openclaw-config.py" "$STAGE_DIR/deploy/patch-openclaw-config.py"
  if [ "$APPLY_CONTAINER_BOUNDARY" = "1" ]; then
    log "Install Docker Compose override with container read/write boundary"
    install -m 0644 "$COMPOSE_OVERRIDE_SRC" "$COMPOSE_OVERRIDE_DST"
  else
    log "Skip Docker Compose override; pass --apply-container-boundary when ready"
  fi
  install -m 0755 "$ENTRYPOINT_SRC" "$ENTRYPOINT_DST"

  write_generated_marker
  reject_staged_runtime_files
}

write_generated_marker() {
  local head status generated_at
  generated_at="$(date -Is)"
  head="$(git_cmd rev-parse --verify HEAD 2>/dev/null || true)"
  status="$(git_cmd status --short || true)"

  cat >"$STAGE_DIR/.OPENCLAW_GENERATED" <<EOF
This directory is generated by /home/joseph/.openclaw/deploy/openclaw-deploy.sh.
Do not edit files here by hand.

Source root: $OC_DIR
Generated at: $generated_at
Git HEAD: ${head:-unavailable}
EOF

  OPENCLAW_MANIFEST_HEAD="$head" \
  OPENCLAW_MANIFEST_STATUS="$status" \
  OPENCLAW_MANIFEST_GENERATED_AT="$generated_at" \
  OPENCLAW_MANIFEST_SOURCE_ROOT="$OC_DIR" \
  OPENCLAW_MANIFEST_STAGE_ROOT="$STAGE_DIR" \
  python3 - <<'PY'
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

source_root = Path(os.environ["OPENCLAW_MANIFEST_SOURCE_ROOT"])
stage_root = Path(os.environ["OPENCLAW_MANIFEST_STAGE_ROOT"])

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def source_for_stage(rel: str) -> str | None:
    if rel.startswith("workspace/"):
        return rel
    if rel.startswith("agent-workspace/"):
        return f"deploy/{rel}"
    if rel == "config/openclaw.template.json":
        return rel
    if rel == "deploy/patch-openclaw-config.py":
        return rel
    return None

files = []
for path in sorted(stage_root.rglob("*")):
    if not path.is_file():
        continue
    rel = path.relative_to(stage_root).as_posix()
    if rel in {".OPENCLAW_GENERATED", ".openclaw-source-manifest.json"}:
        continue
    files.append(
        {
            "path": rel,
            "source": source_for_stage(rel),
            "sha256": sha256(path),
            "bytes": path.stat().st_size,
        }
    )

manifest = {
    "version": 1,
    "generatedAt": os.environ.get("OPENCLAW_MANIFEST_GENERATED_AT", ""),
    "sourceRoot": str(source_root),
    "stageRoot": str(stage_root),
    "gitHead": os.environ.get("OPENCLAW_MANIFEST_HEAD") or None,
    "gitStatusShort": os.environ.get("OPENCLAW_MANIFEST_STATUS", "").splitlines(),
    "files": files,
}
(stage_root / ".openclaw-source-manifest.json").write_text(
    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
}

reject_staged_runtime_files() {
  if find "$STAGE_DIR/workspace" -type f \( \
    -path '*/__pycache__/*' -o \
    -name '*.pyc' -o \
    -name '*.log' -o \
    -name 'state.json' -o \
    -name 'pending-links.json' -o \
    -name 'subscriptions.json' \
  \) -print | grep -q .; then
    find "$STAGE_DIR/workspace" -type f \( \
      -path '*/__pycache__/*' -o \
      -name '*.pyc' -o \
      -name '*.log' -o \
      -name 'state.json' -o \
      -name 'pending-links.json' -o \
      -name 'subscriptions.json' \
    \) -print >&2
    die "staged source contains runtime/cache files; refusing deploy"
  fi
}

sync_source_to_container() {
  local docker
  docker="$(docker_bin)"
  log "Sync staged source into live container volume"
  "$docker" exec "$CONTAINER" mkdir -p \
    /home/joseph/.openclaw/workspace \
    /home/joseph/.openclaw/agent-workspace \
    /home/joseph/.openclaw/agent-workspace/data \
    /home/joseph/.openclaw/agent-workspace/files \
    /home/joseph/.openclaw/runtime \
    /home/joseph/.openclaw/runtime/account-link \
    /home/joseph/.openclaw/runtime/cron-state/algeria-exhibitions-listener \
    /home/joseph/.openclaw/runtime/cron-state/github-issue-tracker \
    /home/joseph/.openclaw/runtime/cron-state/webetu-meals \
    /home/joseph/.openclaw/runtime/public-news \
    /home/joseph/.openclaw/runtime/public-service-gate
  "$docker" cp "$STAGE_DIR/workspace/." "$CONTAINER:/home/joseph/.openclaw/workspace/"
  "$docker" exec "$CONTAINER" chown -R joseph:joseph \
    /home/joseph/.openclaw/workspace \
    /home/joseph/.openclaw/agent-workspace/data \
    /home/joseph/.openclaw/agent-workspace/files \
    /home/joseph/.openclaw/runtime

  for doc in AGENTS.md HEARTBEAT.md IDENTITY.md MEMORY.md README.md SOUL.md TOOLS.md USER.md; do
    if [ -f "$STAGE_DIR/agent-workspace/$doc" ]; then
      "$docker" cp "$STAGE_DIR/agent-workspace/$doc" "$CONTAINER:/home/joseph/.openclaw/agent-workspace/$doc"
    elif [ -f "$STAGE_DIR/workspace/$doc" ]; then
      "$docker" cp "$STAGE_DIR/workspace/$doc" "$CONTAINER:/home/joseph/.openclaw/agent-workspace/$doc"
    fi
  done
  "$docker" cp "$STAGE_DIR/agent-workspace/COMMANDS.md" "$CONTAINER:/home/joseph/.openclaw/agent-workspace/COMMANDS.md"
  "$docker" exec "$CONTAINER" chown root:root \
    /home/joseph/.openclaw/agent-workspace/AGENTS.md \
    /home/joseph/.openclaw/agent-workspace/HEARTBEAT.md \
    /home/joseph/.openclaw/agent-workspace/IDENTITY.md \
    /home/joseph/.openclaw/agent-workspace/MEMORY.md \
    /home/joseph/.openclaw/agent-workspace/README.md \
    /home/joseph/.openclaw/agent-workspace/SOUL.md \
    /home/joseph/.openclaw/agent-workspace/TOOLS.md \
    /home/joseph/.openclaw/agent-workspace/USER.md \
    /home/joseph/.openclaw/agent-workspace/COMMANDS.md
  "$docker" exec "$CONTAINER" chmod 0444 \
    /home/joseph/.openclaw/agent-workspace/AGENTS.md \
    /home/joseph/.openclaw/agent-workspace/HEARTBEAT.md \
    /home/joseph/.openclaw/agent-workspace/IDENTITY.md \
    /home/joseph/.openclaw/agent-workspace/MEMORY.md \
    /home/joseph/.openclaw/agent-workspace/README.md \
    /home/joseph/.openclaw/agent-workspace/SOUL.md \
    /home/joseph/.openclaw/agent-workspace/TOOLS.md \
    /home/joseph/.openclaw/agent-workspace/USER.md \
    /home/joseph/.openclaw/agent-workspace/COMMANDS.md
}

apply_config_template() {
  log "Apply stable live config settings"
  local docker
  docker="$(docker_bin)"

  if "$docker" inspect "$CONTAINER" >/dev/null 2>&1; then
    local tmp_base="/tmp/openclaw-config-patch-$$"
    "$docker" cp "$STAGE_DIR/config/openclaw.template.json" "$CONTAINER:$tmp_base.template.json"
    "$docker" cp "$STAGE_DIR/deploy/patch-openclaw-config.py" "$CONTAINER:$tmp_base.py"
    "$docker" exec -u joseph "$CONTAINER" python3 "$tmp_base.py" \
      "$tmp_base.template.json" \
      "/home/joseph/.openclaw/openclaw.json"
    "$docker" exec "$CONTAINER" rm -f "$tmp_base.template.json" "$tmp_base.py"
    return 0
  fi

  die "container $CONTAINER not reachable; refusing to patch host config as a live deploy"
}

restart_and_check() {
  log "Restart $SERVICE_NAME"
  run_systemctl restart "$SERVICE_NAME"
  run_systemctl is-active "$SERVICE_NAME"

  if [ -x "$DEPLOY_DIR/ops/check-health.sh" ]; then
    log "Run health check"
    "$DEPLOY_DIR/ops/check-health.sh"
  else
    log "Skipping health check; missing $DEPLOY_DIR/ops/check-health.sh"
  fi
}

verify_live_workspace() {
  local docker
  docker="$(docker_bin)"
  log "Verify live workspace setting"
  "$docker" exec "$CONTAINER" python3 - <<'PY'
import json
from pathlib import Path

config = json.loads(Path("/home/joseph/.openclaw/openclaw.json").read_text(encoding="utf-8"))
workspace = config.get("agents", {}).get("defaults", {}).get("workspace")
print(f"agents.defaults.workspace={workspace}")
if workspace != "/home/joseph/.openclaw/agent-workspace":
    raise SystemExit("live container still points at the old workspace")

root = Path(workspace)
expected = ["AGENTS.md", "COMMANDS.md", "README.md", "data", "files"]
missing = [name for name in expected if not (root / name).exists()]
if missing:
    raise SystemExit(f"agent-workspace missing: {', '.join(missing)}")
PY
}

main() {
  parse_args "$@"
  check_gitclaw_disabled
  check_staged_files
  check_docker_access
  syntax_checks
  stage_source
  "$OC_DIR/deploy/check-openclaw-mirror.sh" --strict
  sync_source_to_container
  apply_config_template
  verify_live_workspace
  restart_and_check
  log "Deploy complete. Review Git diff and commit/push manually when ready."
}

main "$@"
