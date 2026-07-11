#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ]; then
  printf 'Usage: %s SOURCE_ROOT STAGE_ROOT\n' "$0" >&2
  exit 2
fi

source_root="${1%/}"
stage_root="${2%/}"
filter_file="$source_root/deploy/stage-workspace.rsync"

for required in \
  "$source_root/workspace" \
  "$source_root/deploy/agent-workspace-admin" \
  "$source_root/deploy/agent-workspace-public" \
  "$source_root/config/openclaw.template.json" \
  "$source_root/deploy/patch-openclaw-config.py" \
  "$source_root/deploy/job-scout-browser.Dockerfile" \
  "$source_root/deploy/job-scout-channel-listener.Dockerfile" \
  "$filter_file"; do
  if [ ! -e "$required" ]; then
    printf 'ERROR: missing staging input: %s\n' "$required" >&2
    exit 1
  fi
done

# Keep the stage root inode stable because Docker bind mounts retain the inode
# they were created against.
mkdir -p \
  "$stage_root/workspace" \
  "$stage_root/agent-workspace-admin" \
  "$stage_root/agent-workspace-public" \
  "$stage_root/config" \
  "$stage_root/deploy"

# Remove the obsolete single-agent context tree after the admin/public split.
# This is generated staging output only; the live private workspace is migrated
# separately by openclaw-deploy.sh before the container is recreated.
if [ -d "$stage_root/agent-workspace" ]; then
  find "$stage_root/agent-workspace" -depth -mindepth 1 -delete
  rmdir "$stage_root/agent-workspace"
fi

rsync -a --delete --filter="merge $filter_file" \
  "$source_root/workspace/" "$stage_root/workspace/"
mkdir -p "$stage_root/workspace/tmp"

rsync -a --delete \
  "$source_root/deploy/agent-workspace-admin/" "$stage_root/agent-workspace-admin/"
rsync -a --delete \
  "$source_root/deploy/agent-workspace-public/" "$stage_root/agent-workspace-public/"
install -m 0644 \
  "$source_root/config/openclaw.template.json" \
  "$stage_root/config/openclaw.template.json"
install -m 0755 \
  "$source_root/deploy/patch-openclaw-config.py" \
  "$stage_root/deploy/patch-openclaw-config.py"
install -m 0644 \
  "$source_root/deploy/job-scout-browser.Dockerfile" \
  "$stage_root/deploy/job-scout-browser.Dockerfile"
install -m 0644 \
  "$source_root/deploy/job-scout-channel-listener.Dockerfile" \
  "$stage_root/deploy/job-scout-channel-listener.Dockerfile"
