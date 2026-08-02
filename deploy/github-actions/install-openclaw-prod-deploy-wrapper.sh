#!/usr/bin/env bash
set -euo pipefail

source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source_wrapper="$source_root/deploy/github-actions/openclaw-prod-deploy"
destination="${OPENCLAW_PROD_WRAPPER:-/usr/local/sbin/openclaw-prod-deploy}"
sudoers_file="${OPENCLAW_PROD_SUDOERS:-/etc/sudoers.d/openclaw-prod-deploy}"
ledger_dir="/var/lib/openclaw-deploy"
ledger_file="$ledger_dir/job-scout-production-ledger.jsonl"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[ "$(id -u)" -eq 0 ] || die "run as root to update the production wrapper"
[ -f "$source_wrapper" ] && [ ! -L "$source_wrapper" ] || die "source wrapper is missing or a symlink"
[ -f "$sudoers_file" ] && [ ! -L "$sudoers_file" ] || die "existing narrow sudoers rule is missing"
command -v visudo >/dev/null 2>&1 || die "visudo is required"
visudo -cf "$sudoers_file" >/dev/null
grep -Fq "$destination" "$sudoers_file" || die "sudoers rule does not name $destination"
if grep -Eq 'NOPASSWD:[[:space:]]*ALL([[:space:]]|$)' "$sudoers_file"; then
  die "refusing broad NOPASSWD: ALL sudoers rule"
fi

tmp="$(mktemp "${destination}.new.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
install -o root -g root -m 0755 "$source_wrapper" "$tmp"
mv -f "$tmp" "$destination"
trap - EXIT

[ "$(stat -c '%U:%G:%a' "$destination")" = "root:root:755" ] \
  || die "installed wrapper ownership or mode is incorrect"
cmp -s "$source_wrapper" "$destination" || die "installed wrapper does not match source"
[ ! -L "$ledger_dir" ] || die "production ledger directory must not be a symlink"
[ ! -e "$ledger_dir" ] || [ -d "$ledger_dir" ] \
  || die "production ledger path is not a directory"
install -d -o root -g root -m 0755 "$ledger_dir"
[ ! -L "$ledger_file" ] || die "production ledger must not be a symlink"
if [ ! -e "$ledger_file" ]; then
  install -o root -g root -m 0600 /dev/null "$ledger_file"
fi
[ -f "$ledger_file" ] && [ ! -L "$ledger_file" ] \
  || die "production ledger is not a regular file"
[ "$(stat -c '%U:%G:%a' "$ledger_file")" = "root:root:600" ] \
  || die "production ledger ownership or mode is incorrect"
printf 'Installed %s with Job Scout request support\n' "$destination"
