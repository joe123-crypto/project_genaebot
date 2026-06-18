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

node --check "$OC_DIR/workspace/plugins/public-service-gate/index.js"
node --check "$OC_DIR/workspace/plugins/whatsapp-relevance-gate/index.js"

pycache="$(mktemp -d)"
trap 'rm -rf "$pycache"' EXIT
PYTHONPYCACHEPREFIX="$pycache" python3 -m py_compile \
  "$OC_DIR/deploy/patch-openclaw-config.py" \
  "$OC_DIR/workspace/account-link/account_link.py" \
  "$OC_DIR/workspace/account-link/pending_cache.py" \
  "$OC_DIR/workspace/public-news/news_subscriptions.py" \
  "$OC_DIR/workspace/cron/algeria-exhibitions-listener/check_new_events.py"

bash -n "$OC_DIR/deploy/openclaw-deploy.sh"
bash -n "$OC_DIR/deploy/container/entrypoint.sh"
bash -n "$OC_DIR/workspace/bin/openclaw-cap"
bash -n "$OC_DIR/workspace/cron/github-issue-tracker/check_issue.sh"
bash -n "$OC_DIR/workspace/cron/webetu-meals/notify-reservations.sh"

python3 -m json.tool "$OC_DIR/config/openclaw.template.json" >/dev/null
python3 -m json.tool "$OC_DIR/workspace/skills.lock.json" >/dev/null

"$OC_DIR/workspace/bin/openclaw-cap" --help >/dev/null

if git -C "$OC_DIR" status --short --ignored=matching | grep -E '^\?\? (openclaw\.json|\.env|credentials/|identity/|memory/|logs/|state/|plugin-state/|agents/|npm/|media/|runtime/)'; then
  die "runtime/private path is visible as untracked; check .gitignore"
fi

printf 'OpenClaw deployment validation OK\n'
