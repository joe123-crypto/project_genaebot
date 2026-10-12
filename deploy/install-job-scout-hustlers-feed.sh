#!/usr/bin/env bash
set -euo pipefail

# Posts ONE job per day from the existing Job Scout group-feed cache into the
# "Hustlers Marketplace" WhatsApp group, alongside the group invite link so
# members can join the dedicated jobs group for more. It reuses the same cache
# that the main group feed (deploy/install-job-scout-group-feed.sh) builds and
# posts from -- the source stays the same -- and delivery is tracked per target,
# so a job posted to the main jobs group is still eligible here (and vice versa).
#
# Configure the target the same way as the main feed: set
# JOB_SCOUT_HUSTLERS_FEED_TARGET to the group JID (120...@g.us), or set
# JOB_SCOUT_HUSTLERS_FEED_GROUP_QUERY / JOB_SCOUT_HUSTLERS_FEED_INVITE_URL once
# the bot is a member and the group appears in the OpenClaw WhatsApp directory.
# The invite link appended to each post defaults to the main jobs-group invite
# (JOB_SCOUT_GROUP_FEED_INVITE_URL); override with JOB_SCOUT_HUSTLERS_FEED_INVITE_URL.

# shellcheck disable=SC1091  # sourced at runtime from the same deploy/ directory
source "$(dirname "${BASH_SOURCE[0]}")/gateway-exec.sh"

NAME="job-scout-hustlers-feed"
# A bit after the main group feed (09:20) so the daily cache is already fresh.
CRON_EXPR="40 9 * * *"
TZ="Africa/Harare"
ALERT_TO="${JOB_SCOUT_FAILURE_ALERT_TO:-+213563719936}"
MAIN_INVITE_URL="${JOB_SCOUT_GROUP_FEED_INVITE_URL:-https://chat.whatsapp.com/JZvh9lwFOvc3N2sSs8F1QD}"
INVITE_URL="${JOB_SCOUT_HUSTLERS_FEED_INVITE_URL:-$MAIN_INVITE_URL}"
TARGET="${JOB_SCOUT_HUSTLERS_FEED_TARGET:-}"
GROUP_QUERY="${JOB_SCOUT_HUSTLERS_FEED_GROUP_QUERY:-Hustlers Marketplace}"
MAX_MESSAGES="${JOB_SCOUT_HUSTLERS_FEED_MAX_MESSAGES:-1}"

openclaw=("${GATEWAY_OPENCLAW[@]}")

resolve_target() {
  local groups_json
  if [ -n "$TARGET" ]; then
    printf '%s\n' "$TARGET"
    return 0
  fi

  if [ -n "$GROUP_QUERY" ]; then
    groups_json="$("${openclaw[@]}" directory groups list --channel whatsapp --query "$GROUP_QUERY" --json --limit 50)"
  else
    groups_json="$("${openclaw[@]}" directory groups list --channel whatsapp --json --limit 200)"
  fi

  GROUPS_JSON="$groups_json" python3 - "$INVITE_URL" "$GROUP_QUERY" <<'PY'
import json
import os
import sys

invite_url = sys.argv[1]
query = sys.argv[2].casefold()
code = invite_url.rstrip("/").split("/")[-1].casefold()
text = os.environ.get("GROUPS_JSON", "")
lines = text.splitlines()
payload = None
for index in range(len(lines)):
  candidate = "\n".join(lines[index:]).strip()
  if not candidate:
    continue
  try:
    payload = json.loads(candidate)
    break
  except json.JSONDecodeError:
    continue
if payload is None:
  raise SystemExit(0)

def walk(value):
  if isinstance(value, dict):
    yield value
    for item in value.values():
      yield from walk(item)
  elif isinstance(value, list):
    for item in value:
      yield from walk(item)

def strings(value):
  if isinstance(value, str):
    yield value
  elif isinstance(value, dict):
    for item in value.values():
      yield from strings(item)
  elif isinstance(value, list):
    for item in value:
      yield from strings(item)

def target_of(group):
  for key in ("id", "groupId", "jid", "target", "chatId"):
    value = str(group.get(key) or "").strip()
    if value.endswith("@g.us"):
      return value
  return ""

matches = []
for group in walk(payload):
  target = target_of(group)
  if not target:
    continue
  haystack = "\n".join(strings(group)).casefold()
  if query and query in haystack:
    matches.append(target)
  elif code and code in haystack:
    matches.append(target)

matches = sorted(set(matches))
if len(matches) == 1:
  print(matches[0])
PY
}

target="$(resolve_target | tail -1)"
if [ -z "$target" ]; then
  printf 'ERROR: could not resolve the "Hustlers Marketplace" WhatsApp group target.\n' >&2
  printf 'Set JOB_SCOUT_HUSTLERS_FEED_TARGET to the group JID, for example 120...@g.us, or set JOB_SCOUT_HUSTLERS_FEED_GROUP_QUERY once the group appears in the OpenClaw WhatsApp directory.\n' >&2
  exit 1
fi
if printf '%s' "$target" | grep -Fq 'chat.whatsapp.com/'; then
  printf 'ERROR: JOB_SCOUT_HUSTLERS_FEED_TARGET must be a WhatsApp group JID, not an invite URL.\n' >&2
  exit 1
fi
case "$target" in
  *@g.us) ;;
  *)
    printf 'ERROR: WhatsApp group target must end with @g.us: %s\n' "$target" >&2
    exit 1
    ;;
esac

COMMAND_ARGV="$(GROUP_TARGET="$target" INVITE_URL="$INVITE_URL" MAX_MESSAGES="$MAX_MESSAGES" python3 - <<'PY'
import json
import os
print(json.dumps([
  "/home/joseph/.openclaw/workspace/bin/job-scout",
  "deliver-group-feed",
  "--target",
  os.environ["GROUP_TARGET"],
  "--invite-url",
  os.environ["INVITE_URL"],
  "--max-messages",
  os.environ["MAX_MESSAGES"],
  "--refresh-if-stale",
], separators=(",", ":")))
PY
)"

"${openclaw[@]}" message send \
  --channel whatsapp \
  --target "$target" \
  --message "Job Scout Hustlers Marketplace feed target check" \
  --dry-run \
  --json >/dev/null

jobs_json="$("${openclaw[@]}" cron list --all --json)"
existing_id="$(printf '%s' "$jobs_json" | jq -r --arg name "$NAME" '.jobs[] | select(.name == $name) | .id' | head -1)"

if [ -n "$existing_id" ]; then
  "${openclaw[@]}" cron edit "$existing_id" \
    --name "$NAME" \
    --cron "$CRON_EXPR" \
    --tz "$TZ" \
    --exact \
    --session isolated \
    --command-argv "$COMMAND_ARGV" \
    --command-cwd /home/joseph/.openclaw/workspace \
    --timeout-seconds 600 \
    --no-deliver \
    --enable >/dev/null
else
  "${openclaw[@]}" cron add \
    --name "$NAME" \
    --cron "$CRON_EXPR" \
    --tz "$TZ" \
    --exact \
    --session isolated \
    --command-argv "$COMMAND_ARGV" \
    --command-cwd /home/joseph/.openclaw/workspace \
    --timeout-seconds 600 \
    --no-deliver \
    --json >/dev/null
fi

jobs_json="$("${openclaw[@]}" cron list --all --json)"
existing_id="$(printf '%s' "$jobs_json" | jq -r --arg name "$NAME" '.jobs[] | select(.name == $name) | .id' | head -1)"

"${openclaw[@]}" cron edit "$existing_id" \
  --failure-alert \
  --failure-alert-after 1 \
  --failure-alert-channel whatsapp \
  --failure-alert-to "$ALERT_TO" \
  --failure-alert-cooldown 1h >/dev/null

"${openclaw[@]}" cron list --all --json | jq -e --arg name "$NAME" --argjson argv "$COMMAND_ARGV" \
  '.jobs[] | select(
    .name == $name and
    .enabled == true and
    .schedule.expr == "40 9 * * *" and
    .schedule.tz == "Africa/Harare" and
    .payload.argv == $argv and
    .failureAlert.after == 1
  )' >/dev/null

printf 'Job Scout Hustlers Marketplace feed scheduled daily at 09:40 Africa/Harare to %s (one job per day).\n' "$target"
