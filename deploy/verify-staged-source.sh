#!/usr/bin/env bash
# Validate a staged live-source tree produced by stage-openclaw-source.sh.
#
# Ported from the reject_staged_runtime_files / require_staged_workspace_files
# guards in openclaw-deploy.sh so the Railway image build (and CI) can fail fast
# if a build ever ships runtime/cache/secret files or is missing a required
# Job Scout deploy artifact. Portable: no docker/systemd/host coupling.
#
# Usage: verify-staged-source.sh STAGE_ROOT
set -euo pipefail

if [ "$#" -ne 1 ]; then
  printf 'Usage: %s STAGE_ROOT\n' "$0" >&2
  exit 2
fi

stage_root="${1%/}"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

# 1. No runtime/cache/secret files may leak into the staged workspace.
if find "$stage_root/workspace" -type f \( \
  -path '*/__pycache__/*' -o \
  -name '*.pyc' -o \
  -name '*.log' -o \
  -name 'state.json' -o \
  -name 'pending-links.json' -o \
  -name 'subscriptions.json' \
\) -print | grep -q .; then
  find "$stage_root/workspace" -type f \( \
    -path '*/__pycache__/*' -o \
    -name '*.pyc' -o \
    -name '*.log' -o \
    -name 'state.json' -o \
    -name 'pending-links.json' -o \
    -name 'subscriptions.json' \
  \) -print >&2
  die "staged source contains runtime/cache files; refusing to build"
fi

# 2. Stable Job Scout deploy entrypoints/artifacts must be present. Internal
#    module layout is validated by the CI test suite, not enumerated here.
missing=0
for path in \
  "$stage_root/workspace/bin/job-scout" \
  "$stage_root/workspace/job-scout/job_scout.py" \
  "$stage_root/workspace/job-scout/web_apply_server.mjs" \
  "$stage_root/deploy/job-scout-browser.Dockerfile" \
  "$stage_root/deploy/job-scout-channel-listener.Dockerfile" \
  "$stage_root/workspace/job-scout/channel-listener/listener.mjs" \
  "$stage_root/workspace/job-scout/channel-listener/package-lock.json"; do
  if [ ! -f "$path" ]; then
    printf 'missing required staged workspace file: %s\n' "$path" >&2
    missing=1
  fi
done
[ "$missing" = "0" ] || die "Job Scout wrapper or implementation is missing from staged source"

printf 'staged source OK: %s\n' "$stage_root"
