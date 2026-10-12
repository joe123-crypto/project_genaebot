#!/usr/bin/env bash
# Shared helper: how to run a command inside the live OpenClaw gateway.
#
# This file is SOURCED by the cron installers, not run directly. It exists so the
# gateway's access mechanism is defined in one place instead of being copied into
# every installer.
#
# Production runs on Railway (project openclaw-gateway, one service: gateway), so
# the default mechanism is `railway ssh --`. There is no Docker daemon and no
# `openclaw-gateway` container there. Set OPENCLAW_EXEC_MODE=docker to target a
# local container instead -- which is also what happens automatically when DOCKER
# or OPENCLAW_CONTAINER is set explicitly, so existing local workflows and the
# installer tests keep working unchanged.
#
# After sourcing, two arrays are available as command prefixes:
#   "${GATEWAY_EXEC[@]}" <argv...>      run argv inside the gateway
#   "${GATEWAY_OPENCLAW[@]}" <args...>  run the openclaw CLI inside the gateway
#
# Usage (from an installer that has already `set -euo pipefail`):
#   source "$(dirname "${BASH_SOURCE[0]}")/gateway-exec.sh"
#   "${GATEWAY_OPENCLAW[@]}" cron list --all --json

# The arrays below are consumed by the sourcing installer, which shellcheck
# cannot see from here.
# shellcheck disable=SC2034

# The openclaw CLI needs the gateway's own auth token, and must not inherit
# OPENCLAW_HOME from the calling shell. Identical under both mechanisms.
_GATEWAY_OPENCLAW_PAYLOAD='exec env -u OPENCLAW_HOME OPENCLAW_GATEWAY_TOKEN="$GATEWAY_AUTH_TOKEN" openclaw "$@"'

if [ -n "${OPENCLAW_EXEC_MODE:-}" ]; then
  _gateway_mode="$OPENCLAW_EXEC_MODE"
elif [ -n "${DOCKER:-}" ] || [ -n "${OPENCLAW_CONTAINER:-}" ]; then
  _gateway_mode="docker"
else
  _gateway_mode="railway"
fi

case "$_gateway_mode" in
  docker)
    _gateway_docker="${DOCKER:-docker}"
    _gateway_container="${OPENCLAW_CONTAINER:-openclaw-gateway}"
    GATEWAY_EXEC=("$_gateway_docker" exec -u joseph "$_gateway_container")
    GATEWAY_OPENCLAW=(
      "$_gateway_docker" exec -u joseph "$_gateway_container"
      sh -lc "$_GATEWAY_OPENCLAW_PAYLOAD" openclaw
    )
    ;;
  railway)
    # `railway ssh` resolves the project/service from the linked directory; the
    # RAILWAY_* overrides let a deploy run from an unlinked checkout.
    _gateway_railway=("${RAILWAY:-railway}" ssh)
    [ -n "${RAILWAY_PROJECT:-}" ] && _gateway_railway+=(--project "$RAILWAY_PROJECT")
    [ -n "${RAILWAY_SERVICE:-}" ] && _gateway_railway+=(--service "$RAILWAY_SERVICE")
    [ -n "${RAILWAY_ENVIRONMENT:-}" ] && _gateway_railway+=(--environment "$RAILWAY_ENVIRONMENT")
    GATEWAY_EXEC=("${_gateway_railway[@]}" --)
    GATEWAY_OPENCLAW=("${_gateway_railway[@]}" -- sh -lc "$_GATEWAY_OPENCLAW_PAYLOAD" openclaw)
    ;;
  *)
    printf 'ERROR: unsupported OPENCLAW_EXEC_MODE: %s (expected railway or docker)\n' "$_gateway_mode" >&2
    exit 2
    ;;
esac

# Exported so a nested installer (install-promo.sh sourcing, pair scripts) sees
# the same resolved mechanism rather than re-deriving it.
GATEWAY_EXEC_MODE="$_gateway_mode"
unset _gateway_mode _gateway_docker _gateway_container _gateway_railway
