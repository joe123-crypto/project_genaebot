#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: BUNDLE_REF=... BROWSER_REF=... LISTENER_REF=... %s REQUEST_FILE CANDIDATE_DIR GITHUB_OUTPUT\n' "$0"
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[ "$#" -eq 3 ] || {
  usage >&2
  exit 2
}

request_file="$1"
candidate_dir="$2"
github_output="$3"
source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
helper="$source_root/deploy/job_scout_release.py"
consumer="$source_root/deploy/job_scout_consumer.py"

[ -f "$request_file" ] && [ ! -L "$request_file" ] || die "request must be a regular file"
[ ! -e "$candidate_dir" ] || die "candidate directory already exists: $candidate_dir"
[ -f "$helper" ] || die "missing release helper: $helper"
[ -f "$consumer" ] || die "missing WS4 consumer: $consumer"

for name in BUNDLE_REF BROWSER_REF LISTENER_REF; do
  [ -n "${!name:-}" ] || die "$name is required"
done
for command in docker python3 sha256sum; do
  command -v "$command" >/dev/null 2>&1 || die "missing required command: $command"
done

scratch="$(mktemp -d)"
container_id=""
cleanup() {
  if [ -n "$container_id" ]; then
    docker rm "$container_id" >/dev/null 2>&1 || true
  fi
  rm -rf "$scratch"
}
trap cleanup EXIT

validate_image() {
  local artifact="$1" reference="$2" inspect_file="$3"
  docker pull --quiet "$reference" >/dev/null
  docker image inspect "$reference" >"$inspect_file"
  python3 "$helper" verify-image \
    --inspect "$inspect_file" \
    --request "$request_file" \
    --artifact "$artifact"
}

validate_image openclaw-bundle "$BUNDLE_REF" "$scratch/bundle-inspect.json"
validate_image browser "$BROWSER_REF" "$scratch/browser-inspect.json"
validate_image channel-listener "$LISTENER_REF" "$scratch/listener-inspect.json"

# Create and export, but never start, the non-running source-bundle image.
container_id="$(docker create "$BUNDLE_REF" /bin/false)"
docker export --output "$scratch/bundle.tar" "$container_id"

mkdir "$candidate_dir"
# docker export writes a CONTAINER filesystem, so the tar also carries files the
# container runtime injects: /etc/mtab (a SYMLINK to /proc/mounts), /.dockerenv,
# and empty /dev, /proc, /sys. None of it belongs to the release, so extract the
# release payload only -- the strict non-regular-file rejection still applies to
# everything inside it.
python3 "$helper" safe-extract \
  --archive "$scratch/bundle.tar" \
  --destination "$candidate_dir/bundle" \
  --only job-scout-manifest.json \
  --only workspace
install -m 0644 "$request_file" "$candidate_dir/job-scout-deploy-request.json"

request_hash="$(sha256sum "$candidate_dir/job-scout-deploy-request.json" | awk '{print $1}')"
python3 "$helper" verify-request \
  --request "$candidate_dir/job-scout-deploy-request.json" \
  --request-sha256 "$request_hash" \
  --bundle "$candidate_dir/bundle"

# WS4 owns the complete overlay allowlist/hash/mode validation. WS5 separately
# binds its canonical manifest fields to the dispatch payload above.
python3 "$consumer" validate "$candidate_dir/bundle"
python3 "$helper" pack-candidate \
  --candidate "$candidate_dir" \
  --archive "${candidate_dir}.tar"
candidate_archive_sha256="$(sha256sum "${candidate_dir}.tar" | awk '{print $1}')"
printf 'candidate_archive_sha256=%s\n' "$candidate_archive_sha256" >>"$github_output"

printf 'Validated immutable Job Scout release %s\n' "$request_hash"
