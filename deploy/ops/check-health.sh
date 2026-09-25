#!/usr/bin/env bash
# Production gateway health check. Optional integrations are advisory only.
set -uo pipefail

CONTAINER="${CONTAINER:-openclaw-gateway}"
GATEWAY_HOST="${GATEWAY_HOST:-127.0.0.1}"
GATEWAY_PORT="${GATEWAY_PORT:-18789}"
DOCKER_BIN="${DOCKER:-}"

if [ -z "$DOCKER_BIN" ]; then
  if command -v /snap/bin/docker >/dev/null 2>&1; then
    DOCKER_BIN=/snap/bin/docker
  else
    DOCKER_BIN=docker
  fi
fi

failures=0
warnings=0

ok() { printf '[OK] %s\n' "$*"; }
skip() { printf '[SKIP] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*"; warnings=$((warnings + 1)); }
fail() { printf '[FAIL] %s\n' "$*"; failures=$((failures + 1)); }

check_gateway_port() {
  python3 - "$GATEWAY_HOST" "$GATEWAY_PORT" <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
try:
    with socket.create_connection((host, port), timeout=3):
        raise SystemExit(0)
except OSError:
    raise SystemExit(1)
PY
}

if ! command -v python3 >/dev/null 2>&1; then
  fail "missing required command: python3"
fi

if ! command -v "$DOCKER_BIN" >/dev/null 2>&1; then
  fail "docker command is unavailable: $DOCKER_BIN"
  exit 1
fi
ok "docker command: $DOCKER_BIN"

if "$DOCKER_BIN" inspect "$CONTAINER" >/dev/null 2>&1; then
  status="$("$DOCKER_BIN" inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null)"
  if [ "$status" = running ]; then
    ok "container $CONTAINER is running"
  else
    fail "container $CONTAINER status is $status"
  fi
else
  fail "container $CONTAINER not found"
fi

if check_gateway_port; then
  ok "gateway port reachable at $GATEWAY_HOST:$GATEWAY_PORT"
else
  fail "gateway port not reachable at $GATEWAY_HOST:$GATEWAY_PORT"
fi

printf '\nRecent WhatsApp/gateway log signals:\n'
if signals="$("$DOCKER_BIN" logs --tail 300 "$CONTAINER" 2>&1 | grep -E "Listening for WhatsApp inbound|Web connection closed|starting provider|gateway] ready" | tail -10)" && \
   [ -n "$signals" ]; then
  printf '%s\n' "$signals"
  ok "found recent gateway/WhatsApp log signals"
else
  warn "no recent gateway/WhatsApp log signals found in last 300 log lines"
fi

printf '\nPublic user count:\n'
if "$DOCKER_BIN" exec -i -u joseph "$CONTAINER" python3 - <<'PY'
import json

p = "/home/joseph/.openclaw/openclaw.json"
with open(p, encoding="utf-8") as fh:
    config = json.load(fh)
users = config.get("plugins", {}).get("entries", {}).get("public-service-gate", {}).get("config", {}).get("publicUsers", [])
print(f"[OK] publicUsers count={len(users)}")
PY
then
  :
else
  fail "could not read publicUsers count"
fi

printf '\nVolume usage from inside container:\n'
if "$DOCKER_BIN" exec "$CONTAINER" sh -lc '
for p in /home/joseph/.openclaw /home/joseph/.openclaw/npm /home/joseph/.openclaw/media; do
  if [ -e "$p" ]; then
    du -sh "$p"
  else
    echo "missing $p"
  fi
done
'; then
  ok "volume usage reported"
else
  warn "could not report all volume usage paths"
fi

printf '\nDocker disk summary:\n'
"$DOCKER_BIN" system df 2>/dev/null || warn "docker system df failed"

if [ "$failures" -gt 0 ]; then
  printf '\nHealth check completed with %s failure(s) and %s warning(s).\n' "$failures" "$warnings"
  exit 1
fi

printf '\nHealth check completed without hard failures (%s warning(s)).\n' "$warnings"
