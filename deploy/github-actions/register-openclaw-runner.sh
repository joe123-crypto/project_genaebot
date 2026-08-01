#!/usr/bin/env bash
set -euo pipefail

source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
repo_url="${OPENCLAW_GITHUB_REPO_URL:-https://github.com/joe123-crypto/project_genaebot}"
runner_dir="${OPENCLAW_RUNNER_DIR:-/home/joseph/actions-runner-openclaw-prod}"
runner_user="${OPENCLAW_RUNNER_USER:-joseph}"
runner_name="${OPENCLAW_RUNNER_NAME:-$(hostname)-openclaw-prod}"
runner_labels="${OPENCLAW_RUNNER_LABELS:-openclaw-prod}"
runner_token="${RUNNER_TOKEN:-${1:-}}"

usage() {
  cat <<EOF
Usage: RUNNER_TOKEN=... $0
   or: $0 RUNNER_TOKEN

Registers the local GitHub Actions runner for:
  $repo_url

The token is the short-lived registration token from:
  Settings -> Actions -> Runners -> New self-hosted runner
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

[ -n "$runner_token" ] || {
  usage >&2
  die "RUNNER_TOKEN is required"
}

[ "$(id -u)" -eq 0 ] || die "run as root so the runner service can be installed"
[ -x "$runner_dir/config.sh" ] || die "missing runner config.sh under $runner_dir"

cd "$runner_dir"
if [ ! -f "$runner_dir/.runner" ]; then
  chown -R "$runner_user:$runner_user" "$runner_dir"
  runuser -u "$runner_user" -- ./config.sh \
    --url "$repo_url" \
    --token "$runner_token" \
    --name "$runner_name" \
    --labels "$runner_labels" \
    --work _work \
    --unattended \
    --replace
fi

[ -x "$runner_dir/svc.sh" ] || die "runner config did not create svc.sh"
./svc.sh install "$runner_user"
./svc.sh start
"$source_root/deploy/github-actions/install-openclaw-prod-deploy-wrapper.sh"
OPENCLAW_RUNNER_DIR="$runner_dir" \
  "$source_root/deploy/github-actions/install-openclaw-runner-hardening.sh" --restart
./svc.sh status
