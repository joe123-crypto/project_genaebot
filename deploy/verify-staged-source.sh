#!/usr/bin/env bash
# Validate a staged live-source tree produced by stage-openclaw-source.sh.
#
# Fails the Railway image build (and CI) fast if a build ever ships
# runtime/cache/secret files, or is missing a required service entrypoint. Portable: no docker/systemd/host coupling.
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
  -name 'state.json' \
\) -print | grep -q .; then
  find "$stage_root/workspace" -type f \( \
    -path '*/__pycache__/*' -o \
    -name '*.pyc' -o \
    -name '*.log' -o \
    -name 'state.json' \
  \) -print >&2
  die "staged source contains runtime/cache files; refusing to build"
fi

# 2. The CV maker wrapper and implementation must be present. Internal module
#    layout is validated by the CI test suite, not enumerated here.
missing=0
for path in \
  "$stage_root/workspace/bin/cv-maker" \
  "$stage_root/workspace/cv-maker/cv_maker.py"; do
  if [ ! -f "$path" ]; then
    printf 'missing required staged workspace file: %s\n' "$path" >&2
    missing=1
  fi
done
[ "$missing" = "0" ] || die "a service wrapper or implementation is missing from staged source"

printf 'staged source OK: %s\n' "$stage_root"
