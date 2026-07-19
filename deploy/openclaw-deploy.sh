#!/usr/bin/env bash
set -euo pipefail

OC_DIR="${OPENCLAW_DIR:-/home/joseph/.openclaw}"
DEPLOY_DIR="${OPENCLAW_DEPLOY_DIR:-/home/joseph/openclaw-deploy}"
STAGE_DIR="$DEPLOY_DIR/live-source"
COMPOSE_OVERRIDE_SRC="$OC_DIR/deploy/docker-compose.override.yml"
COMPOSE_OVERRIDE_DST="$DEPLOY_DIR/docker-compose.override.yml"
ENTRYPOINT_SRC="$OC_DIR/deploy/container/entrypoint.sh"
ENTRYPOINT_DST="$DEPLOY_DIR/entrypoint.sh"
GATEWAY_DOCKERFILE_SRC="$OC_DIR/deploy/container/Dockerfile"
GATEWAY_DOCKERFILE_DST="$DEPLOY_DIR/Dockerfile"
SERVICE_NAME="${OPENCLAW_CONTAINER_SERVICE:-openclaw-gateway-container.service}"
SERVICE_UNIT_SRC="$OC_DIR/deploy/systemd/openclaw-gateway-container.service"
SERVICE_UNIT_DST="/etc/systemd/system/$SERVICE_NAME"
DOCKER_WAIT_SRC="$OC_DIR/deploy/systemd/wait-for-docker.sh"
DOCKER_WAIT_DST="/usr/local/libexec/openclaw-wait-for-docker"
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

run_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
    return
  fi
  if command -v sudo >/dev/null 2>&1 && sudo -n "$@"; then
    return
  fi
  die "root command failed: $*"
}

require_file() {
  [ -f "$1" ] || die "missing required file: $1"
}

# The container boundary is active when the compose override is installed in the
# deploy dir. Under it, workspace + context MDs reach the container as read-only
# bind mounts and runtime/agent-workspace are writable host bind mounts, so the
# in-container `docker cp` source sync is both redundant and impossible (ro).
boundary_active() {
  [ -f "$COMPOSE_OVERRIDE_DST" ]
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

install_container_service() {
  require_file "$SERVICE_UNIT_SRC"
  require_file "$DOCKER_WAIT_SRC"

  log "Install source-controlled container service and Docker readiness check"
  run_root install -D -m 0755 "$DOCKER_WAIT_SRC" "$DOCKER_WAIT_DST"
  run_root install -D -m 0644 "$SERVICE_UNIT_SRC" "$SERVICE_UNIT_DST"
  run_systemctl daemon-reload
  run_systemctl enable "$SERVICE_NAME"
}

container_status() {
  local docker="$1"
  "$docker" inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || return 1
}

wait_container_running() {
  local docker="$1"
  local timeout="${2:-45}"
  local started status elapsed
  started="$(date +%s)"

  while :; do
    status="$(container_status "$docker" || true)"
    if [ "$status" = "running" ] && "$docker" exec "$CONTAINER" true >/dev/null 2>&1; then
      return 0
    fi

    elapsed=$(($(date +%s) - started))
    if [ "$elapsed" -ge "$timeout" ]; then
      [ -n "$status" ] || status="missing"
      printf '%s\n' "$status"
      return 1
    fi

    sleep 1
  done
}

ensure_container_running() {
  local docker status
  docker="$(docker_bin)"

  if wait_container_running "$docker" 10 >/dev/null; then
    return 0
  fi

  status="$(container_status "$docker" || true)"
  [ -n "$status" ] || status="missing"
  log "Container $CONTAINER is $status before config patch; restarting $SERVICE_NAME"
  run_systemctl restart "$SERVICE_NAME"

  if ! status="$(wait_container_running "$docker" 60)"; then
    die "container $CONTAINER did not reach running state after restart (last status: $status)"
  fi
}

recreate_boundary_stack() {
  local docker
  docker="$(docker_bin)"
  boundary_active || return 0
  install -d -o joseph -g joseph -m 0700 \
    /home/joseph/projects/genaebot/agent-workspace-admin \
    /home/joseph/projects/genaebot/agent-workspace-admin/data \
    /home/joseph/projects/genaebot/agent-workspace-admin/files \
    /home/joseph/projects/genaebot/agent-workspace-public \
    /home/joseph/projects/genaebot/agent-workspace-public/data \
    /home/joseph/projects/genaebot/agent-workspace-public/files
  find /home/joseph/projects/genaebot/agent-workspace-public -maxdepth 1 -type f \
    \( -name MEMORY.md -o -name COMMANDS.md \) \
    -delete
  if [ -d /home/joseph/projects/genaebot/agent-workspace ] && \
     [ ! -e /home/joseph/projects/genaebot/agent-workspace-admin/.migrated-from-agent-workspace ]; then
    log "Migrate existing private agent workspace into agent-workspace-admin"
    rsync -a /home/joseph/projects/genaebot/agent-workspace/ /home/joseph/projects/genaebot/agent-workspace-admin/
    touch /home/joseph/projects/genaebot/agent-workspace-admin/.migrated-from-agent-workspace
    chown -R joseph:joseph /home/joseph/projects/genaebot/agent-workspace-admin
  fi
  install -d -o joseph -g joseph -m 0700 /home/joseph/projects/genaebot/runtime/job-scout
  install -d -o joseph -g joseph -m 0700 \
    /home/joseph/projects/genaebot/runtime/job-scout-channel \
    /home/joseph/projects/genaebot/runtime/job-scout-channel/auth \
    /home/joseph/projects/genaebot/runtime/job-scout-channel/media
  log "Build and recreate the container stack so bind mounts use the current live-source inode"
  (cd "$DEPLOY_DIR" && "$docker" compose up -d --build --force-recreate)
  if ! wait_container_running "$docker" 90 >/dev/null; then
    die "container $CONTAINER did not become ready after stack recreation"
  fi
}

start_optional_job_scout_browser() {
  local docker token browser_id health
  docker="$(docker_bin)"
  boundary_active || return 0

  if ! token="$(
    cd "$DEPLOY_DIR" && \
    "$docker" compose --profile job-scout config --format json | \
    python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["job-scout-browser"]["environment"].get("JOB_SCOUT_BROWSER_TOKEN", ""))'
  )"; then
    log "WARNING: could not resolve optional Job Scout browser configuration; core gateway remains available"
    return 0
  fi
  if [ "${#token}" -lt 32 ]; then
    log "Job Scout browser not started: JOB_SCOUT_BROWSER_TOKEN is missing or shorter than 32 characters"
    (cd "$DEPLOY_DIR" && "$docker" compose --profile job-scout stop job-scout-browser >/dev/null 2>&1) || true
    return 0
  fi
  unset token

  log "Build and start optional Job Scout browser"
  if ! (cd "$DEPLOY_DIR" && "$docker" compose --profile job-scout up -d --build --force-recreate job-scout-browser); then
    log "WARNING: Job Scout browser failed to build or start; core gateway remains available"
    (cd "$DEPLOY_DIR" && "$docker" compose --profile job-scout stop job-scout-browser >/dev/null 2>&1) || true
    return 0
  fi

  browser_id="$(cd "$DEPLOY_DIR" && "$docker" compose --profile job-scout ps -q job-scout-browser)"
  for _attempt in $(seq 1 18); do
    health="$("$docker" inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$browser_id" 2>/dev/null || true)"
    if [ "$health" = "healthy" ]; then
      log "Job Scout browser is ready"
      return 0
    fi
    if [ "$health" = "unhealthy" ]; then
      break
    fi
    sleep 5
  done
  log "WARNING: Job Scout browser is not ready (health=${health:-missing}); scheduler installation remains blocked"
  return 0
}

start_optional_job_scout_channel_listener() {
  local docker listener_id health auth_file
  docker="$(docker_bin)"
  boundary_active || return 0
  auth_file="/home/joseph/projects/genaebot/runtime/job-scout-channel/auth/creds.json"
  if [ ! -s "$auth_file" ]; then
    log "Job Scout channel listener not started: run deploy/pair-job-scout-channel.sh to scan its separate QR code"
    (cd "$DEPLOY_DIR" && "$docker" compose --profile job-scout-channel stop job-scout-channel-listener >/dev/null 2>&1) || true
    return 0
  fi
  log "Build and start Job Scout channel listener"
  if ! (cd "$DEPLOY_DIR" && "$docker" compose --profile job-scout-channel up -d --build --force-recreate job-scout-channel-listener); then
    log "WARNING: Job Scout channel listener failed to build or start; core gateway remains available"
    return 0
  fi
  listener_id="$(cd "$DEPLOY_DIR" && "$docker" compose --profile job-scout-channel ps -q job-scout-channel-listener)"
  for _attempt in $(seq 1 24); do
    health="$("$docker" inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$listener_id" 2>/dev/null || true)"
    if [ "$health" = "healthy" ]; then
      log "Job Scout channel listener is ready"
      bash "$OC_DIR/deploy/install-job-scout-channel-health.sh"
      return 0
    fi
    if [ "$health" = "unhealthy" ]; then
      break
    fi
    sleep 5
  done
  log "WARNING: Job Scout channel listener is not ready (health=${health:-missing})"
  return 0
}

syntax_checks() {
  log "Portable source validation"
  "$OC_DIR/deploy/ci-check.sh"
}

stage_source() {
  require_file "$COMPOSE_OVERRIDE_SRC"
  require_file "$ENTRYPOINT_SRC"
  require_file "$GATEWAY_DOCKERFILE_SRC"
  log "Stage curated source into $STAGE_DIR"
  bash "$OC_DIR/deploy/stage-openclaw-source.sh" "$OC_DIR" "$STAGE_DIR"
  if [ "$APPLY_CONTAINER_BOUNDARY" = "1" ] || boundary_active; then
    log "Install Docker Compose override with container read/write boundary"
    install -m 0644 "$COMPOSE_OVERRIDE_SRC" "$COMPOSE_OVERRIDE_DST"
  else
    log "Skip Docker Compose override; pass --apply-container-boundary when ready"
  fi
  install -m 0755 "$ENTRYPOINT_SRC" "$ENTRYPOINT_DST"
  install -m 0644 "$GATEWAY_DOCKERFILE_SRC" "$GATEWAY_DOCKERFILE_DST"

  write_generated_marker
  reject_staged_runtime_files
  require_staged_workspace_files
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
    if rel.startswith(("agent-workspace-admin/", "agent-workspace-public/")):
        return f"deploy/{rel}"
    if rel == "config/openclaw.template.json":
        return rel
    if rel == "deploy/patch-openclaw-config.py":
        return rel
    if rel == "deploy/job-scout-browser.Dockerfile":
        return rel
    if rel == "deploy/job-scout-channel-listener.Dockerfile":
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

require_staged_workspace_files() {
  local missing=0
  # Stable deploy entrypoints/artifacts only. Internal module layout
  # (jobscout/, scrapers/) is validated by the CI test suite, not enumerated
  # here, so restructuring internals never trips this deploy sanity check.
  for path in \
    "$STAGE_DIR/workspace/bin/job-scout" \
    "$STAGE_DIR/workspace/job-scout/job_scout.py" \
    "$STAGE_DIR/workspace/job-scout/web_apply_server.mjs" \
    "$STAGE_DIR/deploy/job-scout-browser.Dockerfile" \
    "$STAGE_DIR/deploy/job-scout-channel-listener.Dockerfile" \
    "$STAGE_DIR/workspace/job-scout/channel-listener/listener.mjs" \
    "$STAGE_DIR/workspace/job-scout/channel-listener/package-lock.json"; do
    if [ ! -f "$path" ]; then
      printf 'missing required staged workspace file: %s\n' "$path" >&2
      missing=1
    fi
  done
  if [ "$missing" = "1" ]; then
    die "Job Scout wrapper or implementation is missing from staged source"
  fi
}

sync_source_to_container() {
  local docker
  docker="$(docker_bin)"
  if boundary_active; then
    log "Container boundary active; workspace + context come from read-only bind mounts. Skipping in-container source sync."
    return 0
  fi
  log "Sync staged source into live container volume"
  "$docker" exec "$CONTAINER" mkdir -p \
    /home/joseph/.openclaw/workspace \
    /home/joseph/.openclaw/agent-workspace-admin \
    /home/joseph/.openclaw/agent-workspace-admin/data \
    /home/joseph/.openclaw/agent-workspace-admin/files \
    /home/joseph/.openclaw/agent-workspace-public \
    /home/joseph/.openclaw/agent-workspace-public/data \
    /home/joseph/.openclaw/agent-workspace-public/files \
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
    /home/joseph/.openclaw/agent-workspace-admin/data \
    /home/joseph/.openclaw/agent-workspace-admin/files \
    /home/joseph/.openclaw/agent-workspace-public/data \
    /home/joseph/.openclaw/agent-workspace-public/files \
    /home/joseph/.openclaw/runtime

  for doc in AGENTS.md COMMANDS.md README.md; do
    if [ -f "$STAGE_DIR/agent-workspace-admin/$doc" ]; then
      "$docker" cp "$STAGE_DIR/agent-workspace-admin/$doc" "$CONTAINER:/home/joseph/.openclaw/agent-workspace-admin/$doc"
    fi
  done
  for doc in HEARTBEAT.md IDENTITY.md MEMORY.md SOUL.md TOOLS.md USER.md; do
    if [ -f "$STAGE_DIR/workspace/$doc" ]; then
      "$docker" cp "$STAGE_DIR/workspace/$doc" "$CONTAINER:/home/joseph/.openclaw/agent-workspace-admin/$doc"
    fi
  done
  for doc in AGENTS.md README.md; do
    if [ -f "$STAGE_DIR/agent-workspace-public/$doc" ]; then
      "$docker" cp "$STAGE_DIR/agent-workspace-public/$doc" "$CONTAINER:/home/joseph/.openclaw/agent-workspace-public/$doc"
    elif [ -f "$STAGE_DIR/workspace/$doc" ]; then
      "$docker" cp "$STAGE_DIR/workspace/$doc" "$CONTAINER:/home/joseph/.openclaw/agent-workspace-public/$doc"
    fi
  done
  "$docker" exec "$CONTAINER" chown -R joseph:joseph \
    /home/joseph/.openclaw/agent-workspace-admin \
    /home/joseph/.openclaw/agent-workspace-public
}

apply_config_template() {
  log "Apply stable live config settings"
  local docker
  docker="$(docker_bin)"

  ensure_container_running

  if "$docker" inspect "$CONTAINER" >/dev/null 2>&1; then
    local tmp_base="/tmp/openclaw-config-patch-$$"
    "$docker" cp "$STAGE_DIR/config/openclaw.template.json" "$CONTAINER:$tmp_base.template.json"
    "$docker" cp "$STAGE_DIR/deploy/patch-openclaw-config.py" "$CONTAINER:$tmp_base.py"
    "$docker" cp "$STAGE_DIR/workspace/docs/public-whatsapp-system-prompt.txt" "$CONTAINER:$tmp_base.prompt.txt"
    "$docker" exec -u joseph "$CONTAINER" python3 "$tmp_base.py" \
      "$tmp_base.template.json" \
      "/home/joseph/.openclaw/openclaw.json" \
      "$tmp_base.prompt.txt"
    "$docker" exec "$CONTAINER" rm -f \
      "$tmp_base.template.json" \
      "$tmp_base.py" \
      "$tmp_base.prompt.txt"
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
    local health_log
    health_log="$(mktemp)"
    if ! "$DEPLOY_DIR/ops/check-health.sh" 2>&1 | tee "$health_log"; then
      if is_isolated_webetu_health_failure "$health_log"; then
        log "Health check warning: isolated Webetu worker probe failed; gateway deploy checks passed"
        rm -f "$health_log"
        return 0
      fi
      diagnose_health_failure || true
      rm -f "$health_log"
      return 1
    fi
    rm -f "$health_log"
  else
    log "Skipping health check; missing $DEPLOY_DIR/ops/check-health.sh"
  fi
}

is_isolated_webetu_health_failure() {
  local health_log="$1"
  local fail_count

  fail_count="$(grep -c '^\[FAIL\]' "$health_log" || true)"
  [ "$fail_count" = "1" ] || return 1
  grep -Fqx '[FAIL] webetu worker list-registrations failed' "$health_log"
}

diagnose_health_failure() {
  local docker
  docker="$(docker_bin)"

  log "Health check failed; collecting Webetu worker diagnostics"
  "$docker" inspect -f 'container={{.Name}} status={{.State.Status}} restarting={{.State.Restarting}} exitCode={{.State.ExitCode}}' "$CONTAINER" 2>&1 || true
  "$docker" exec "$CONTAINER" sh -lc 'ls -l /usr/local/bin/webetu-worker-safe /opt/webetu-worker/.venv/bin/webetu-worker /etc/webetu-worker/config.json /var/lib/webetu-worker 2>&1' || true
  "$docker" exec "$CONTAINER" sh -lc '
    out="$(mktemp)"
    err="$(mktemp)"
    if runuser -u webetu-worker -- /usr/local/bin/webetu-worker-safe list-registrations >"$out" 2>"$err"; then
      rc=0
    else
      rc=$?
    fi
    printf "webetu-list-registrations rc=%s\n" "$rc"
    printf "webetu-list-registrations stdout:\n"
    sed "s/[[:space:]]\\+/ /g" "$out" | head -20
    printf "webetu-list-registrations stderr:\n"
    sed "s/[[:space:]]\\+/ /g" "$err" | head -20
    rm -f "$out" "$err"
    exit "$rc"
  ' || true
}

verify_live_workspace() {
  local docker rc _attempt
  docker="$(docker_bin)"
  log "Verify live agent workspace split"
  rc=1
  for _attempt in $(seq 1 5); do
    set +e
    "$docker" exec -i "$CONTAINER" python3 - <<'PY'
import json
from pathlib import Path

config = json.loads(Path("/home/joseph/.openclaw/openclaw.json").read_text(encoding="utf-8"))
agents = config.get("agents", {})
listed = {entry.get("id"): entry for entry in agents.get("list", [])}
admin = listed.get("main", {})
public = listed.get("public", {})
if admin.get("workspace") != "/home/joseph/.openclaw/agent-workspace-admin":
    raise SystemExit("main agent workspace is not isolated")
if public.get("workspace") != "/home/joseph/.openclaw/agent-workspace-public":
    raise SystemExit("public agent workspace is not isolated")
for workspace, expected in {
    admin["workspace"]: ["AGENTS.md", "COMMANDS.md", "README.md", "MEMORY.md", "USER.md", "data", "files"],
    public["workspace"]: ["AGENTS.md", "HEARTBEAT.md", "README.md", "TOOLS.md", "USER.md"],
}.items():
    root = Path(workspace)
    missing = [name for name in expected if not (root / name).exists()]
    if missing:
        raise SystemExit(f"{workspace} missing: {', '.join(missing)}")
public_root = Path(public["workspace"])
for forbidden in ("MEMORY.md", "COMMANDS.md"):
    if (public_root / forbidden).exists():
        raise SystemExit(f"public workspace contains forbidden context: {forbidden}")

job_scout = Path("/home/joseph/.openclaw/workspace/bin/job-scout")
if not job_scout.is_file():
    raise SystemExit("read-only workspace mount is missing bin/job-scout")
runtime = Path("/home/joseph/.openclaw/runtime/job-scout")
runtime.mkdir(parents=True, exist_ok=True)
probe = runtime / ".deploy-write-check"
probe.write_text("ok\n", encoding="utf-8")
probe.unlink()
PY
    rc=$?
    set -e
    if [ "$rc" -eq 0 ]; then
      return 0
    fi
    log "Live workspace verification attempt $_attempt failed (rc=$rc); waiting for the container to stabilize"
    sleep 2
    ensure_container_running
  done

  return "$rc"
}

main() {
  parse_args "$@"
  check_gitclaw_disabled
  check_staged_files
  check_docker_access
  syntax_checks
  install_container_service
  stage_source
  "$OC_DIR/deploy/check-openclaw-mirror.sh" --strict
  recreate_boundary_stack
  ensure_container_running
  sync_source_to_container
  apply_config_template
  verify_live_workspace
  restart_and_check
  bash "$OC_DIR/deploy/install-job-scout-cv-conversion.sh"
  start_optional_job_scout_browser
  start_optional_job_scout_channel_listener
  log "Deploy complete. Review Git diff and commit/push manually when ready."
}

main "$@"
