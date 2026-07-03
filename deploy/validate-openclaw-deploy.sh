#!/usr/bin/env bash
set -euo pipefail

OC_DIR="${OPENCLAW_DIR:-/home/joseph/.openclaw}"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

if crontab -l 2>/dev/null | grep -Fq '/home/joseph/.openclaw/gitclaw/auto_backup.sh'; then
  die "GitClaw autocommit cron entry is still active"
fi

git -C "$OC_DIR" rev-parse --is-inside-work-tree >/dev/null
"$OC_DIR/deploy/ci-check.sh"
"$OC_DIR/deploy/check-openclaw-mirror.sh"

if git -C "$OC_DIR" status --short --ignored=matching | grep -E '^\?\? (openclaw\.json|\.env|credentials/|identity/|memory/|logs/|state/|plugin-state/|agents/|npm/|media/|runtime/)'; then
  die "runtime/private path is visible as untracked; check .gitignore"
fi

printf 'OpenClaw deployment validation OK\n'
