#!/usr/bin/env bash
# Reserve Webetu meals for each onboarded user and DM that user only.
#
# Credential handling stays inside the isolated webetu-worker process. This
# script only sees sanitized worker JSON plus the registered WhatsApp target.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${WEBETU_NOTIFY_LOG_FILE:-/home/joseph/.openclaw/runtime/cron-state/webetu-meals/notification.log}"
STATE_DIR="${WEBETU_NOTIFY_STATE_DIR:-$(dirname "$LOG_FILE")}"
SKIP_WINDOWS_FILE="${WEBETU_NOTIFY_SKIP_WINDOWS_FILE:-$STATE_DIR/skip-windows.tsv}"
LOCK_DIR="${WEBETU_NOTIFY_LOCK_DIR:-$STATE_DIR/notify.lock}"
FAILED_DELIVERY_DIR="${WEBETU_FAILED_DELIVERY_DIR:-$STATE_DIR/failed-deliveries}"
JOURNAL_TOOL="${WEBETU_DELIVERY_JOURNAL_TOOL:-$SCRIPT_DIR/delivery_journal.py}"
WORKER="${WEBETU_WORKER_BIN:-/home/joseph/projects/webetu/webetu-worker/bin/webetu-worker}"
WORKER_CONFIG="${WEBETU_WORKER_CONFIG:-/home/joseph/projects/webetu/webetu-worker/config/webetu-worker.example.json}"
WORKER_MODE="${WEBETU_WORKER_MODE:-sudo}"
WORKER_SAFE="${WEBETU_WORKER_SAFE:-/usr/local/bin/webetu-worker-safe}"
OPENCLAW_HOME="${OPENCLAW_HOME:-/home/joseph}"
if [ -f "$OPENCLAW_HOME/openclaw.json" ]; then
  OPENCLAW_CONFIG="${OPENCLAW_CONFIG:-$OPENCLAW_HOME/openclaw.json}"
  OPENCLAW_HOME="$(dirname "$OPENCLAW_HOME")"
else
  OPENCLAW_CONFIG="${OPENCLAW_CONFIG:-$OPENCLAW_HOME/.openclaw/openclaw.json}"
fi
OPENCLAW_BIN="${OPENCLAW_BIN:-}"
if [ -z "$OPENCLAW_BIN" ]; then
  if command -v openclaw >/dev/null 2>&1; then
    OPENCLAW_BIN="$(command -v openclaw)"
  elif [ -x /home/joseph/.npm-global/bin/openclaw ]; then
    OPENCLAW_BIN="/home/joseph/.npm-global/bin/openclaw"
  elif [ -x /usr/local/bin/openclaw ]; then
    OPENCLAW_BIN="/usr/local/bin/openclaw"
  else
    OPENCLAW_BIN="openclaw"
  fi
fi
OPENCLAW_RUN_USER="${OPENCLAW_RUN_USER:-joseph}"
ADMIN_TARGET="${WEBETU_ADMIN_TARGET:-+213563719936}"
DRY_RUN="${WEBETU_NOTIFY_DRY_RUN:-0}"
HEARTBEAT_SECONDS="${WEBETU_NOTIFY_HEARTBEAT_SECONDS:-10}"
WORKER_STEP_TIMEOUT_SECONDS="${WEBETU_WORKER_STEP_TIMEOUT_SECONDS:-90}"
REGISTRATION_MAX_ATTEMPTS="${WEBETU_REGISTRATION_MAX_ATTEMPTS:-3}"
RESERVATION_MAX_ATTEMPTS="${WEBETU_RESERVATION_MAX_ATTEMPTS:-2}"
FIRST_RETRY_DELAY_SECONDS="${WEBETU_FIRST_RETRY_DELAY_SECONDS:-30}"
SECOND_RETRY_DELAY_SECONDS="${WEBETU_SECOND_RETRY_DELAY_SECONDS:-60}"
USER_FAILURE_MESSAGE="${WEBETU_USER_FAILURE_MESSAGE:-I couldn't complete today's Webetu meal reservation automatically. Please reserve manually today.}"
BROAD_FAILURE_NOTICE_ALLOWED="${WEBETU_ALLOW_BROAD_FAILURE_NOTICE:-0}"

ensure_state_dir() {
  mkdir -p "$STATE_DIR" "$(dirname "$LOG_FILE")"
}

log() {
  local line
  ensure_state_dir
  line="$(printf '[%s] %s' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$*")"
  printf '%s\n' "$line" >>"$LOG_FILE"
  printf '%s\n' "$line"
}

acquire_run_lock() {
  ensure_state_dir
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    printf '%s\n' "$$" >"$LOCK_DIR/pid"
    return 0
  fi

  local lock_pid=""
  if [ -f "$LOCK_DIR/pid" ]; then
    lock_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  fi
  if [ -n "$lock_pid" ] && ! kill -0 "$lock_pid" 2>/dev/null; then
    rm -rf "$LOCK_DIR"
    if mkdir "$LOCK_DIR" 2>/dev/null; then
      printf '%s\n' "$$" >"$LOCK_DIR/pid"
      log "lock recovered stale_pid=$lock_pid"
      return 0
    fi
  fi

  log "skipped reason=run_lock_active lock_pid=${lock_pid:-unknown}"
  printf 'Webetu notify skipped: another run is already active.\n'
  return 1
}

release_run_lock() {
  if [ -d "$LOCK_DIR" ] && [ "$(cat "$LOCK_DIR/pid" 2>/dev/null || true)" = "$$" ]; then
    rm -rf "$LOCK_DIR"
  fi
}

skip_window_marked() {
  local window="$*"
  local line marked
  [ -f "$SKIP_WINDOWS_FILE" ] || return 1

  while IFS= read -r line || [ -n "$line" ]; do
    marked="${line%%#*}"
    marked="$(printf '%s' "$marked" | xargs)"
    [ -z "$marked" ] && continue
    [ "$marked" = "$window" ] && return 0
  done <"$SKIP_WINDOWS_FILE"

  return 1
}

run_worker() {
  if [ "$WORKER_MODE" = "sudo" ]; then
    sudo -n -u webetu-worker "$WORKER_SAFE" "$@"
    return $?
  fi

  "$WORKER" --config "$WORKER_CONFIG" "$@"
}

run_worker_capture() {
  local step="$1"
  local output_file="$2"
  shift 2

  local started_at now elapsed pid rc sleep_pid
  started_at="$(date +%s)"
  log "worker start step=$step timeout_seconds=$WORKER_STEP_TIMEOUT_SECONDS"

  run_worker "$@" >"$output_file" 2>&1 &
  pid=$!

  while kill -0 "$pid" 2>/dev/null; do
    now="$(date +%s)"
    elapsed=$((now - started_at))
    if [ "$elapsed" -ge "$WORKER_STEP_TIMEOUT_SECONDS" ]; then
      log "worker stuck step=$step elapsed_seconds=$elapsed action=terminate"
      kill -TERM "$pid" 2>/dev/null || true
      sleep 2
      if kill -0 "$pid" 2>/dev/null; then
        log "worker stuck step=$step elapsed_seconds=$elapsed action=kill"
        kill -KILL "$pid" 2>/dev/null || true
      fi
      wait "$pid" 2>/dev/null || true
      log "worker done step=$step status=timeout rc=124 elapsed_seconds=$elapsed"
      return 124
    fi

    sleep "$HEARTBEAT_SECONDS" &
    sleep_pid=$!
    wait "$sleep_pid" 2>/dev/null || true
    if kill -0 "$pid" 2>/dev/null; then
      now="$(date +%s)"
      elapsed=$((now - started_at))
      log "heartbeat step=$step elapsed_seconds=$elapsed"
    fi
  done

  if wait "$pid"; then
    rc=0
  else
    rc=$?
  fi

  now="$(date +%s)"
  elapsed=$((now - started_at))
  log "worker done step=$step rc=$rc elapsed_seconds=$elapsed"
  return "$rc"
}

retry_delay_seconds() {
  local failed_attempt="$1"

  if [ "$failed_attempt" -le 1 ]; then
    printf '%s\n' "$FIRST_RETRY_DELAY_SECONDS"
    return
  fi
  printf '%s\n' "$SECOND_RETRY_DELAY_SECONDS"
}

is_transient_worker_failure() {
  local rc="$1"
  local output_file="$2"

  if [ "$rc" -eq 124 ]; then
    return 0
  fi

  grep -Eiq \
    'temporary failure in name resolution|name or service not known|network is unreachable|connection (timed out|timeout|reset|refused|aborted)|timed out|timeout|temporar(y|ily) unavailable|service unavailable|remote end closed|remote disconnected|broken pipe|http[^0-9]*(408|429|5[0-9][0-9])|status code[^0-9]*(408|429|5[0-9][0-9])' \
    "$output_file"
}

run_registration_capture() {
  local output_file="$1"
  shift

  local attempt=1 rc delay
  while [ "$attempt" -le "$REGISTRATION_MAX_ATTEMPTS" ]; do
    if run_worker_capture "list-registrations attempt=$attempt/$REGISTRATION_MAX_ATTEMPTS" "$output_file" "$@"; then
      return 0
    else
      rc=$?
    fi

    if [ "$attempt" -ge "$REGISTRATION_MAX_ATTEMPTS" ]; then
      log "worker retry exhausted step=list-registrations attempts=$attempt rc=$rc"
      return "$rc"
    fi

    delay="$(retry_delay_seconds "$attempt")"
    log "worker retry step=list-registrations failed_attempt=$attempt/$REGISTRATION_MAX_ATTEMPTS rc=$rc reason=lookup_failure delay_seconds=$delay"
    sleep "$delay"
    attempt=$((attempt + 1))
  done
}

run_reservation_capture() {
  local step="$1"
  local output_file="$2"
  shift 2

  local attempt=1 rc delay
  while [ "$attempt" -le "$RESERVATION_MAX_ATTEMPTS" ]; do
    if run_worker_capture "$step attempt=$attempt/$RESERVATION_MAX_ATTEMPTS" "$output_file" "$@"; then
      return 0
    else
      rc=$?
    fi

    if ! is_transient_worker_failure "$rc" "$output_file"; then
      log "worker retry skipped step=$step attempt=$attempt/$RESERVATION_MAX_ATTEMPTS rc=$rc reason=permanent_failure"
      return "$rc"
    fi

    if [ "$attempt" -ge "$RESERVATION_MAX_ATTEMPTS" ]; then
      log "worker retry exhausted step=$step attempts=$attempt rc=$rc reason=transient_failure"
      return "$rc"
    fi

    delay="$(retry_delay_seconds "$attempt")"
    log "worker retry step=$step failed_attempt=$attempt/$RESERVATION_MAX_ATTEMPTS rc=$rc reason=transient_failure delay_seconds=$delay"
    sleep "$delay"
    attempt=$((attempt + 1))
  done
}

run_openclaw() {
  if [ "$(id -u)" = "0" ] && id "$OPENCLAW_RUN_USER" >/dev/null 2>&1; then
    runuser -u "$OPENCLAW_RUN_USER" -- env HOME="$OPENCLAW_HOME" OPENCLAW_HOME="$OPENCLAW_HOME" "$OPENCLAW_BIN" "$@"
    return $?
  fi

  HOME="$OPENCLAW_HOME" OPENCLAW_HOME="$OPENCLAW_HOME" "$OPENCLAW_BIN" "$@"
}

require_whatsapp_connected() {
  if [ "$DRY_RUN" = "1" ]; then
    return 0
  fi

  local status_file
  status_file="$(mktemp)"
  if ! run_openclaw channels status --json >"$status_file" 2>&1; then
    log "whatsapp preflight failed reason=status_command_failed"
    rm -f "$status_file"
    return 1
  fi

  if ! python3 - "$status_file" <<'PY'
import json
import pathlib
import sys

text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
try:
    payload = json.loads(text)
except json.JSONDecodeError:
    decoder = json.JSONDecoder()
    payload = None
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    if payload is None:
        raise SystemExit(1)

accounts = payload.get("channelAccounts", {}).get("whatsapp", [])
default = next((item for item in accounts if item.get("accountId") == "default"), None)
if not default:
    raise SystemExit(1)
if not all(default.get(field) is True for field in ("linked", "running", "connected")):
    raise SystemExit(1)
PY
  then
    log "whatsapp preflight failed reason=default_account_not_connected"
    rm -f "$status_file"
    return 1
  fi

  rm -f "$status_file"
  log "whatsapp preflight ok account=default"
}

journal_command() {
  python3 "$JOURNAL_TOOL" "$@"
}

journal_prepare() {
  local user_id="$1"
  local phone_hash="$2"
  local source="$3"
  local message_file="$4"
  shift 4

  local delivery_id date
  delivery_id="$(date -u '+%Y%m%dT%H%M%S%NZ')-${phone_hash}-$$"
  local -a args=(
    prepare
    --directory "$FAILED_DELIVERY_DIR"
    --delivery-id "$delivery_id"
    --user-id "$user_id"
    --phone-hash "$phone_hash"
    --source "$source"
    --message-file "$message_file"
  )
  for date in "$@"; do
    args+=(--date "$date")
  done
  journal_command "${args[@]}"
  printf '%s\n' "$delivery_id"
}

registration_rows() {
  local registrations_file="$1"

  python3 - "$OPENCLAW_CONFIG" "$registrations_file" <<'PY'
import hashlib
import json
import pathlib
import re
import sys

config_path = pathlib.Path(sys.argv[1]).expanduser()
registrations_path = pathlib.Path(sys.argv[2])

def normalize_phone(value):
    value = str(value or "").strip()
    digits = re.sub(r"\D", "", value)
    if not digits:
        return ""
    normalized = "+" + digits
    if len(normalized) < 9 or len(normalized) > 16:
        return ""
    return normalized

def load_json_object(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                payload, _ = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
        raise

payload = load_json_object(registrations_path.read_text(encoding="utf-8", errors="replace"))
if payload.get("status") != "ok":
    raise SystemExit("worker registration listing did not return status=ok")

allowlisted = set()
allow_all_whatsapp = False
if config_path.is_file():
    config = json.loads(config_path.read_text(encoding="utf-8"))
    whatsapp = config.get("channels", {}).get("whatsapp", {})
    for value in whatsapp.get("allowFrom", []) or []:
        if str(value or "").strip() == "*":
            allow_all_whatsapp = True
            continue
        phone = normalize_phone(value)
        if phone:
            allowlisted.add(phone)
    direct = whatsapp.get("direct", {})
    if isinstance(direct, dict):
        for value in direct.keys():
            if str(value or "").strip() == "*":
                allow_all_whatsapp = True
                continue
            phone = normalize_phone(value)
            if phone:
                allowlisted.add(phone)

for record in payload.get("registrations", []):
    phone = normalize_phone(record.get("phone"))
    if not phone:
        continue
    phone_hash = str(record.get("phone_hash") or "")[:12]
    if not phone_hash:
        phone_hash = hashlib.sha256(f"whatsapp:{phone}".encode("utf-8")).hexdigest()[:12]
    fields = [
        str(record.get("user_id") or ""),
        phone,
        phone_hash,
        str(record.get("registration_status") or ""),
        "yes" if allow_all_whatsapp or phone in allowlisted else "no",
        str(record.get("source") or "firestore"),
    ]
    print("\t".join(fields))
PY
}

registration_ready() {
  case "$1" in
    shared|active)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

format_result_line() {
  local date="$1"
  local rc="$2"
  local result_file="$3"

  python3 - "$date" "$rc" "$result_file" <<'PY'
import json
import pathlib
import re
import sys

date = sys.argv[1]
rc = int(sys.argv[2])
text = pathlib.Path(sys.argv[3]).read_text(encoding="utf-8", errors="replace").strip()

def compact(value, limit=130):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(value) > limit:
        value = value[: limit - 3].rstrip() + "..."
    return value

def load_json_object(value):
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, char in enumerate(value):
            if char != "{":
                continue
            try:
                payload, _ = decoder.raw_decode(value[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
        raise

try:
    payload = load_json_object(text)
except json.JSONDecodeError:
    payload = {"date": date, "status": "error", "message": "Worker returned non-JSON output."}

status = compact(payload.get("status") or ("error" if rc else "unknown"), 50)
message = compact(payload.get("message"), 130)
meals = payload.get("meals_reserved")
date = compact(payload.get("date") or date, 20)
restaurant = compact(payload.get("restaurant"), 80)
restaurant_suffix = f" at {restaurant}" if restaurant else ""

if rc == 0 and status in {"success", "already_booked", "dry_run"}:
    if meals is None:
        print(f"- {date}: OK {status}{restaurant_suffix}")
    else:
        print(f"- {date}: OK {status} ({meals}/3 meals){restaurant_suffix}")
elif rc == 0:
    print(f"- {date}: Not completed automatically. Please reserve manually for this date.")
else:
    print(f"- {date}: Not completed automatically. Please reserve manually for this date.")
PY
}

result_status() {
  local date="$1"
  local rc="$2"
  local result_file="$3"

  python3 - "$date" "$rc" "$result_file" <<'PY'
import json
import pathlib
import sys

date = sys.argv[1]
rc = int(sys.argv[2])
text = pathlib.Path(sys.argv[3]).read_text(encoding="utf-8", errors="replace").strip()
def load_json_object(value):
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, char in enumerate(value):
            if char != "{":
                continue
            try:
                payload, _ = decoder.raw_decode(value[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
        raise

try:
    payload = load_json_object(text)
    print(str(payload.get("status") or ("error" if rc else "unknown")))
except json.JSONDecodeError:
    print("error")
PY
}

send_message() {
  local phone="$1"
  local phone_hash="$2"
  local message_file="$3"
  local user_id="$4"
  local source="$5"
  shift 5
  local output delivery_id output_file

  if [ "$DRY_RUN" = "1" ]; then
    log "delivery dry-run phone_hash=$phone_hash"
    printf '%s\n' "--- dry-run delivery phone_hash=$phone_hash ---"
    cat "$message_file"
    printf '\n'
    return 0
  fi

  if ! delivery_id="$(journal_prepare "$user_id" "$phone_hash" "$source" "$message_file" "$@")"; then
    log "delivery blocked phone_hash=$phone_hash reason=journal_prepare_failed"
    return 1
  fi
  if ! journal_command mark-attempt --directory "$FAILED_DELIVERY_DIR" --delivery-id "$delivery_id"; then
    log "delivery blocked phone_hash=$phone_hash delivery_id=$delivery_id reason=journal_mark_attempt_failed"
    return 1
  fi

  output_file="$(mktemp)"
  if run_openclaw message send --channel whatsapp --target "$phone" --message "$(cat "$message_file")" --json >"$output_file" 2>&1; then
    if ! journal_command mark-delivered --directory "$FAILED_DELIVERY_DIR" --delivery-id "$delivery_id"; then
      log "delivery unresolved phone_hash=$phone_hash delivery_id=$delivery_id reason=journal_archive_failed"
      rm -f "$output_file"
      return 1
    fi
    log "delivery ok phone_hash=$phone_hash"
    rm -f "$output_file"
    return 0
  fi

  output="$(cat "$output_file")"
  output="${output//$phone/[redacted-phone]}"
  printf '%s\n' "$output" >"$output_file"
  journal_command mark-failed \
    --directory "$FAILED_DELIVERY_DIR" \
    --delivery-id "$delivery_id" \
    --error-file "$output_file" || log "delivery journal update failed phone_hash=$phone_hash delivery_id=$delivery_id"
  log "delivery failed phone_hash=$phone_hash output=$(printf '%s' "$output" | tr '\n' ' ' | cut -c1-240)"
  rm -f "$output_file"
  return 1
}

send_admin_alert() {
  local reason="$1"
  local message

  message="$(printf 'Webetu notifier needs attention.\n\n%s\n\nLog: %s\n' "$reason" "$LOG_FILE")"
  if [ "$DRY_RUN" = "1" ]; then
    log "admin-alert dry-run reason=$(printf '%s' "$reason" | tr '\n' ' ' | cut -c1-180)"
    return 0
  fi

  if [ ! -x "$OPENCLAW_BIN" ]; then
    log "admin-alert skipped reason=openclaw_missing"
    return 1
  fi

  run_openclaw message send --channel whatsapp --target "$ADMIN_TARGET" --message "$message" --json >/dev/null 2>&1
}

fail_system() {
  local reason="$1"
  log "system failure reason=$(printf '%s' "$reason" | tr '\n' ' ' | cut -c1-220)"
  send_admin_alert "$reason" || true
  printf '%s\n' "$reason" >&2
  return 1
}

send_failure_notices() {
  log "failure-notice started dry_run=$DRY_RUN"

  local registrations_file
  registrations_file="$(mktemp)"
  if ! run_registration_capture "$registrations_file" list-registrations; then
    rm -f "$registrations_file"
    fail_system "Failed to list users for Webetu failure notices."
    return 1
  fi

  local rows
  if ! rows="$(registration_rows "$registrations_file" 2>>"$LOG_FILE")"; then
    rm -f "$registrations_file"
    fail_system "Failed to parse users for Webetu failure notices."
    return 1
  fi
  rm -f "$registrations_file"

  if [ -z "$rows" ]; then
    log "failure-notice no onboarded users"
    printf 'No onboarded Webetu users for failure notice.\n'
    return 0
  fi

  local processed=0
  local blocked=0
  local delivery_failures=0
  local skipped=0
  local user_id phone phone_hash registration_status allowed source message_file

  while IFS=$'\t' read -r user_id phone phone_hash registration_status allowed source; do
    [ -z "$user_id" ] && continue
    source="${source:-firestore}"

    if ! registration_ready "$registration_status"; then
      log "failure-notice skip user=$user_id phone_hash=$phone_hash source=$source reason=registration_status:$registration_status"
      skipped=$((skipped + 1))
      continue
    fi

    if [ "$allowed" != "yes" ]; then
      log "failure-notice blocked user=$user_id phone_hash=$phone_hash source=$source reason=not_whatsapp_allowlisted"
      blocked=$((blocked + 1))
      continue
    fi

    processed=$((processed + 1))
    message_file="$(mktemp)"
    printf '%s\n' "$USER_FAILURE_MESSAGE" >"$message_file"
    log "failure-notice delivery start user=$user_id phone_hash=$phone_hash source=$source"
    if ! send_message "$phone" "$phone_hash" "$message_file" "$user_id" "$source"; then
      delivery_failures=$((delivery_failures + 1))
    fi
    log "failure-notice delivery done user=$user_id phone_hash=$phone_hash source=$source"
    rm -f "$message_file"
  done <<<"$rows"

  log "failure-notice completed processed=$processed skipped=$skipped blocked=$blocked delivery_failures=$delivery_failures"
  printf 'Webetu failure notice completed: processed=%s skipped=%s blocked=%s delivery_failures=%s\n' "$processed" "$skipped" "$blocked" "$delivery_failures"

  if [ "$blocked" -gt 0 ] || [ "$delivery_failures" -gt 0 ]; then
    send_admin_alert "Failure notice delivery issue: blocked=$blocked delivery_failures=$delivery_failures." || true
    return 1
  fi
}

list_failed_deliveries() {
  journal_command list --directory "$FAILED_DELIVERY_DIR"
}

retry_failed_delivery() {
  local delivery_id="$1"
  local message_file metadata user_id expected_phone_hash expected_source
  local registrations_file rows
  local current_user_id phone phone_hash registration_status allowed source
  local matched_phone=""

  if ! require_whatsapp_connected; then
    log "delivery retry blocked delivery_id=$delivery_id reason=whatsapp_not_connected"
    return 1
  fi

  message_file="$(mktemp)"
  if ! metadata="$(journal_command extract \
    --directory "$FAILED_DELIVERY_DIR" \
    --delivery-id "$delivery_id" \
    --message-file "$message_file")"; then
    rm -f "$message_file"
    return 1
  fi
  IFS=$'\t' read -r user_id expected_phone_hash expected_source <<<"$metadata"

  registrations_file="$(mktemp)"
  if ! run_registration_capture "$registrations_file" list-registrations; then
    rm -f "$registrations_file" "$message_file"
    log "delivery retry blocked delivery_id=$delivery_id phone_hash=$expected_phone_hash reason=registration_lookup_failed"
    return 1
  fi
  if ! rows="$(registration_rows "$registrations_file" 2>>"$LOG_FILE")"; then
    rm -f "$registrations_file" "$message_file"
    log "delivery retry blocked delivery_id=$delivery_id phone_hash=$expected_phone_hash reason=registration_parse_failed"
    return 1
  fi
  rm -f "$registrations_file"

  while IFS=$'\t' read -r current_user_id phone phone_hash registration_status allowed source; do
    [ "$current_user_id" = "$user_id" ] || continue
    [ "$phone_hash" = "$expected_phone_hash" ] || continue
    registration_ready "$registration_status" || continue
    [ "$allowed" = "yes" ] || continue
    matched_phone="$phone"
    break
  done <<<"$rows"

  if [ -z "$matched_phone" ]; then
    rm -f "$message_file"
    log "delivery retry blocked delivery_id=$delivery_id phone_hash=$expected_phone_hash reason=registration_not_current_or_allowlisted"
    return 1
  fi

  if ! journal_command mark-attempt --directory "$FAILED_DELIVERY_DIR" --delivery-id "$delivery_id"; then
    rm -f "$message_file"
    log "delivery retry blocked delivery_id=$delivery_id phone_hash=$expected_phone_hash reason=journal_mark_attempt_failed"
    return 1
  fi

  local output_file output
  output_file="$(mktemp)"
  if run_openclaw message send \
    --channel whatsapp \
    --target "$matched_phone" \
    --message "$(cat "$message_file")" \
    --json >"$output_file" 2>&1; then
    rm -f "$message_file" "$output_file"
    if ! journal_command mark-delivered --directory "$FAILED_DELIVERY_DIR" --delivery-id "$delivery_id"; then
      log "delivery retry unresolved delivery_id=$delivery_id phone_hash=$expected_phone_hash reason=journal_archive_failed"
      return 1
    fi
    log "delivery retry ok delivery_id=$delivery_id phone_hash=$expected_phone_hash"
    return 0
  fi

  output="$(cat "$output_file")"
  output="${output//$matched_phone/[redacted-phone]}"
  printf '%s\n' "$output" >"$output_file"
  journal_command mark-failed \
    --directory "$FAILED_DELIVERY_DIR" \
    --delivery-id "$delivery_id" \
    --error-file "$output_file" || log "delivery retry journal update failed delivery_id=$delivery_id phone_hash=$expected_phone_hash"
  rm -f "$message_file" "$output_file"
  log "delivery retry failed delivery_id=$delivery_id phone_hash=$expected_phone_hash output=$(printf '%s' "$output" | tr '\n' ' ' | cut -c1-240)"
  return 1
}

main() {
  ensure_state_dir

  if [ ! -f "$JOURNAL_TOOL" ]; then
    log "system failure reason=delivery_journal_tool_not_found"
    return 1
  fi

  if [ "${1:-}" = "--list-failed" ]; then
    [ "$#" -eq 1 ] || {
      printf '%s\n' '--list-failed does not accept additional arguments' >&2
      return 2
    }
    list_failed_deliveries
    return $?
  fi

  if [ "${1:-}" = "--retry-failed" ]; then
    [ "$#" -eq 2 ] || {
      printf '%s\n' '--retry-failed requires exactly one delivery id' >&2
      return 2
    }
    if ! acquire_run_lock; then
      return 1
    fi
    trap release_run_lock EXIT
    retry_failed_delivery "$2"
    return $?
  fi

  if [ "${1:-}" = "--failure-notice-only" ] && [ "$BROAD_FAILURE_NOTICE_ALLOWED" != "1" ]; then
    log "failure-notice refused reason=broad_failure_notice_disabled"
    printf 'Broad Webetu failure notices are disabled. Set WEBETU_ALLOW_BROAD_FAILURE_NOTICE=1 only for a confirmed full-system outage.\n' >&2
    return 1
  fi

  if [ "${1:-}" = "--failure-notice-only" ]; then
    shift
    if ! require_whatsapp_connected; then
      log "failure-notice blocked reason=whatsapp_not_connected"
      return 1
    fi
    send_failure_notices
    return $?
  fi

  local dates=("$@")
  if [ "${#dates[@]}" -eq 0 ]; then
    dates=(
      "$(date -d tomorrow +%Y-%m-%d)"
      "$(date -d 'tomorrow + 1 day' +%Y-%m-%d)"
      "$(date -d 'tomorrow + 2 days' +%Y-%m-%d)"
    )
  fi

  if skip_window_marked "${dates[@]}"; then
    log "skipped reason=window_suppressed dates=${dates[*]}"
    printf 'Webetu notify skipped: date window is suppressed (%s).\n' "${dates[*]}"
    return 0
  fi

  if ! acquire_run_lock; then
    return 0
  fi
  trap release_run_lock EXIT

  if [ "$WORKER_MODE" = "sudo" ] && [ ! -x "$WORKER_SAFE" ]; then
    log "system failure reason=webetu-worker_safe_wrapper_not_found"
    printf 'webetu-worker safe wrapper not found\n' >&2
    return 1
  fi

  if [ "$WORKER_MODE" != "sudo" ] && [ ! -x "$WORKER" ]; then
    log "system failure reason=webetu-worker_executable_not_found"
    printf 'webetu-worker executable not found\n' >&2
    return 1
  fi

  if [ "$DRY_RUN" != "1" ] && [ ! -x "$OPENCLAW_BIN" ]; then
    log "system failure reason=openclaw_executable_not_found"
    printf 'openclaw executable not found\n' >&2
    return 1
  fi

  if ! require_whatsapp_connected; then
    log "system failure reason=whatsapp_not_connected action=abort_before_reservations"
    return 1
  fi

  log "started dates=${dates[*]} dry_run=$DRY_RUN worker_mode=$WORKER_MODE heartbeat_seconds=$HEARTBEAT_SECONDS worker_step_timeout_seconds=$WORKER_STEP_TIMEOUT_SECONDS registration_max_attempts=$REGISTRATION_MAX_ATTEMPTS reservation_max_attempts=$RESERVATION_MAX_ATTEMPTS"

  local registrations_file
  registrations_file="$(mktemp)"
  if ! run_registration_capture "$registrations_file" list-registrations; then
    rm -f "$registrations_file"
    fail_system "Failed to list onboarded Webetu users."
    return 1
  fi

  local rows
  if ! rows="$(registration_rows "$registrations_file" 2>>"$LOG_FILE")"; then
    rm -f "$registrations_file"
    fail_system "Failed to parse onboarded Webetu users."
    return 1
  fi
  rm -f "$registrations_file"

  if [ -z "$rows" ]; then
    log "no onboarded users"
    printf 'No onboarded Webetu users.\n'
    return 0
  fi

  local processed=0
  local blocked=0
  local delivery_failures=0
  local skipped=0
  local reservation_failures=0
  local user_id phone phone_hash registration_status allowed source

  while IFS=$'\t' read -r user_id phone phone_hash registration_status allowed source; do
    [ -z "$user_id" ] && continue
    source="${source:-firestore}"

    if ! registration_ready "$registration_status"; then
      log "skip user=$user_id phone_hash=$phone_hash source=$source reason=registration_status:$registration_status"
      skipped=$((skipped + 1))
      continue
    fi

    if [ "$allowed" != "yes" ]; then
      log "blocked user=$user_id phone_hash=$phone_hash source=$source reason=not_whatsapp_allowlisted"
      blocked=$((blocked + 1))
      continue
    fi

    processed=$((processed + 1))
    log "user start user=$user_id phone_hash=$phone_hash source=$source"
    local summary_file
    summary_file="$(mktemp)"
    {
      printf 'Webetu meals - %s\n\n' "$(date '+%Y-%m-%d')"
      printf 'Reservation results:\n'
    } >"$summary_file"

    local date result_file rc status line
    local user_reservation_failed=0
    for date in "${dates[@]}"; do
      result_file="$(mktemp)"
      log "reserve start user=$user_id phone_hash=$phone_hash source=$source date=$date"
      local -a reserve_args=(reserve --user "$user_id" --date "$date")
      if [ "$DRY_RUN" = "1" ]; then
        reserve_args+=(--dry-run)
      fi
      if run_reservation_capture "reserve user=$user_id phone_hash=$phone_hash source=$source date=$date" "$result_file" "${reserve_args[@]}"; then
        rc=0
      else
        rc=$?
      fi

      status="$(result_status "$date" "$rc" "$result_file")"
      line="$(format_result_line "$date" "$rc" "$result_file")"
      printf '%s\n' "$line" >>"$summary_file"
      log "reserve done user=$user_id phone_hash=$phone_hash source=$source date=$date status=$status rc=$rc"
      if [ "$rc" -ne 0 ] || { [ "$status" != "success" ] && [ "$status" != "already_booked" ] && [ "$status" != "dry_run" ]; }; then
        user_reservation_failed=1
      fi
      rm -f "$result_file"
    done

    {
      printf '\n'
      printf 'This message is only for your Webetu account.\n'
    } >>"$summary_file"

    if [ "$user_reservation_failed" -ne 0 ]; then
      reservation_failures=$((reservation_failures + 1))
    fi

    log "delivery start user=$user_id phone_hash=$phone_hash source=$source"
    if ! send_message "$phone" "$phone_hash" "$summary_file" "$user_id" "$source" "${dates[@]}"; then
      delivery_failures=$((delivery_failures + 1))
    fi
    if [ "$user_reservation_failed" -ne 0 ]; then
      log "user done user=$user_id phone_hash=$phone_hash source=$source status=reservation_failure_summary_sent"
    else
      log "user done user=$user_id phone_hash=$phone_hash source=$source"
    fi
    rm -f "$summary_file"
  done <<<"$rows"

  log "completed processed=$processed skipped=$skipped blocked=$blocked reservation_failures=$reservation_failures delivery_failures=$delivery_failures"
  printf 'Webetu notify completed: processed=%s skipped=%s blocked=%s reservation_failures=%s delivery_failures=%s\n' "$processed" "$skipped" "$blocked" "$reservation_failures" "$delivery_failures"

  if [ "$reservation_failures" -gt 0 ] || [ "$blocked" -gt 0 ] || [ "$delivery_failures" -gt 0 ]; then
    send_admin_alert "Run issue: reservation_failures=$reservation_failures blocked=$blocked delivery_failures=$delivery_failures. Per-user reservation summaries were sent where delivery was possible." || true
    return 1
  fi
}

main "$@"
