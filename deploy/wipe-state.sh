#!/usr/bin/env bash
#
# wipe-state.sh — reset the live OpenClaw volume to a fresh start.
#
# This clears the agent's ACCUMULATED RUNTIME STATE (session/conversation
# history, the daily memory/ entries, logs, caches, and cron run-logs) so the
# Genaie Service Desk agent wakes up with a clean slate — WITHOUT destroying its
# curated identity, secrets/config, or real service-user data.
#
# WHAT IT WIPES (contents cleared, the directory itself recreated empty):
#   - memory/                 daily YYYY-MM-DD.md entries + heartbeat-state.json
#                             (the curated MEMORY.md lives in workspace/ and is
#                              NOT here, so it is never touched)
#   - state/ sessions/ openclaw-runtime/   session / conversation / gateway state
#   - flows/ tasks/ delivery-queue/ agents/  transient queues + run state
#   - logs/ media/inbound media/outbound voice-calls/ cache/ browser/  logs/caches/media
#   - runtime/cron-state/*/notification.log, stale */notify.lock/, runtime/jobscout-runs.txt
#
# WHAT IT PRESERVES (never touched):
#   - identity/ credentials/ devices/ .env openclaw.json   identity, secrets, config
#   - runtime/public-service-gate/invites.json (+ state.json, *.bak-*)   service users
#   - runtime/account-link/pending-links.json               pending onboarding links
#   - plugin-state/                                          plugin state (e.g. gate unlocks)
#   - workspace/ agent-workspace/ (incl. both MEMORY.md), cron entrypoint scripts
#
# USAGE:
#   bash deploy/wipe-state.sh            # DRY RUN — prints what would happen, changes nothing
#   bash deploy/wipe-state.sh --apply    # actually wipe
#   OC_DIR=/path/to/.openclaw bash deploy/wipe-state.sh --apply
#
# IMPORTANT: run this on the deployed host/container, with the OpenClaw service
# STOPPED, so nothing rewrites state mid-wipe.
#
set -euo pipefail

OC_DIR="${OC_DIR:-$HOME/.openclaw}"
APPLY=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

OWNER="${OC_OWNER:-joseph:joseph}"

if [ "$APPLY" = "1" ]; then
  MODE="APPLY"
else
  MODE="DRY-RUN (no changes — pass --apply to execute)"
fi
echo "== wipe-state.sh =="
echo "OC_DIR : $OC_DIR"
echo "mode   : $MODE"
echo

# Guard: must point at a seeded OpenClaw volume (matches entrypoint.sh's check).
if [ ! -f "$OC_DIR/openclaw.json" ]; then
  echo "FATAL: $OC_DIR/openclaw.json not found — refusing to wipe an unseeded/unknown dir." >&2
  exit 78
fi

run() {
  # run <description> <command...>
  local desc="$1"; shift
  echo "  $desc"
  if [ "$APPLY" = "1" ]; then
    "$@"
  fi
}

# Empty a directory's contents but keep (or recreate) the directory itself.
clear_dir() {
  local dir="$1"
  if [ ! -d "$dir" ]; then
    echo "  [skip] $dir (absent)"
    return
  fi
  if [ -z "$(ls -A "$dir" 2>/dev/null)" ]; then
    echo "  [skip] $dir (already empty)"
    return
  fi
  echo "  [wipe] contents of $dir/*"
  if [ "$APPLY" = "1" ]; then
    find "$dir" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  fi
}

remove_path() {
  local p="$1"
  if [ -e "$p" ]; then
    echo "  [wipe] $p"
    [ "$APPLY" = "1" ] && rm -rf "$p"
  else
    echo "  [skip] $p (absent)"
  fi
}

echo "WIPE — session / conversation / runtime state:"
for d in state sessions openclaw-runtime flows tasks delivery-queue agents; do
  clear_dir "$OC_DIR/$d"
done

echo
echo "WIPE — accumulated memory (daily entries + heartbeat; MEMORY.md preserved):"
clear_dir "$OC_DIR/memory"

echo
echo "WIPE — logs / caches / media:"
for d in logs cache browser voice-calls media/inbound media/outbound; do
  clear_dir "$OC_DIR/$d"
done

echo
echo "WIPE — cron run-logs / locks (state dirs themselves kept):"
if [ -d "$OC_DIR/runtime/cron-state" ]; then
  while IFS= read -r f; do remove_path "$f"; done < <(find "$OC_DIR/runtime/cron-state" -type f -name 'notification.log' 2>/dev/null)
  while IFS= read -r f; do remove_path "$f"; done < <(find "$OC_DIR/runtime/cron-state" -type d -name 'notify.lock' 2>/dev/null)
fi
remove_path "$OC_DIR/runtime/jobscout-runs.txt"

echo
echo "RECREATE wiped dirs (owner: $OWNER):"
for d in memory state openclaw-runtime flows tasks delivery-queue agents \
         logs cache voice-calls media/inbound media/outbound; do
  run "mkdir -p $OC_DIR/$d" mkdir -p "$OC_DIR/$d"
done
if [ "$APPLY" = "1" ]; then
  for d in memory state openclaw-runtime flows tasks delivery-queue agents logs cache voice-calls media; do
    [ -e "$OC_DIR/$d" ] && chown -R "$OWNER" "$OC_DIR/$d" 2>/dev/null || true
  done
fi

echo
echo "PRESERVED (untouched):"
for p in identity credentials devices .env openclaw.json plugin-state \
         runtime/public-service-gate/invites.json \
         runtime/public-service-gate/state.json \
         runtime/account-link/pending-links.json; do
  if [ -e "$OC_DIR/$p" ]; then echo "  [keep] $OC_DIR/$p"; fi
done

echo
if [ "$APPLY" = "1" ]; then
  echo "Done. Start the OpenClaw service for a fresh session."
else
  echo "Dry run complete. Re-run with --apply to perform the wipe."
fi
