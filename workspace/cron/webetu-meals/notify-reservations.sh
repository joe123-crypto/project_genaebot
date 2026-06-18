#!/usr/bin/env bash
# Reserve Webetu meals for each onboarded user and DM that user only.
#
# Credential handling stays inside the isolated webetu-worker process. This
# script only sees sanitized worker JSON plus the registered WhatsApp target.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${WEBETU_NOTIFY_LOG_FILE:-/home/joseph/.openclaw/runtime/cron-state/webetu-meals/notification.log}"
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
USER_FAILURE_MESSAGE="${WEBETU_USER_FAILURE_MESSAGE:-I couldn't complete today's Webetu meal reservation automatically. Please reserve manually today.}"

log() {
  local line
  line="$(printf '[%s] %s' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$*")"
  printf '%s\n' "$line" >>"$LOG_FILE"
  printf '%s\n' "$line"
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

run_openclaw() {
  if [ "$(id -u)" = "0" ] && id "$OPENCLAW_RUN_USER" >/dev/null 2>&1; then
    runuser -u "$OPENCLAW_RUN_USER" -- env HOME="$OPENCLAW_HOME" OPENCLAW_HOME="$OPENCLAW_HOME" "$OPENCLAW_BIN" "$@"
    return $?
  fi

  HOME="$OPENCLAW_HOME" OPENCLAW_HOME="$OPENCLAW_HOME" "$OPENCLAW_BIN" "$@"
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
  local output

  if [ "$DRY_RUN" = "1" ]; then
    log "delivery dry-run phone_hash=$phone_hash"
    printf '%s\n' "--- dry-run delivery phone_hash=$phone_hash ---"
    cat "$message_file"
    printf '\n'
    return 0
  fi

  if output="$(run_openclaw message send --channel whatsapp --target "$phone" --message "$(cat "$message_file")" --json 2>&1)"; then
    log "delivery ok phone_hash=$phone_hash"
    return 0
  fi

  output="${output//$phone/[redacted-phone]}"
  log "delivery failed phone_hash=$phone_hash output=$(printf '%s' "$output" | tr '\n' ' ' | cut -c1-240)"
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
  if ! run_worker_capture "failure-notice list-registrations" "$registrations_file" list-registrations; then
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
    if ! send_message "$phone" "$phone_hash" "$message_file"; then
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

main() {
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

  if [ "${1:-}" = "--failure-notice-only" ]; then
    shift
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

  log "started dates=${dates[*]} dry_run=$DRY_RUN worker_mode=$WORKER_MODE heartbeat_seconds=$HEARTBEAT_SECONDS worker_step_timeout_seconds=$WORKER_STEP_TIMEOUT_SECONDS"

  local registrations_file
  registrations_file="$(mktemp)"
  if ! run_worker_capture "list-registrations" "$registrations_file" list-registrations; then
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
      if run_worker_capture "reserve user=$user_id phone_hash=$phone_hash source=$source date=$date" "$result_file" "${reserve_args[@]}"; then
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
      log "delivery skipped user=$user_id phone_hash=$phone_hash source=$source reason=reservation_failure"
      log "user done user=$user_id phone_hash=$phone_hash source=$source status=reservation_failure"
      rm -f "$summary_file"
      continue
    fi

    log "delivery start user=$user_id phone_hash=$phone_hash source=$source"
    if ! send_message "$phone" "$phone_hash" "$summary_file"; then
      delivery_failures=$((delivery_failures + 1))
    fi
    log "user done user=$user_id phone_hash=$phone_hash source=$source"
    rm -f "$summary_file"
  done <<<"$rows"

  log "completed processed=$processed skipped=$skipped blocked=$blocked reservation_failures=$reservation_failures delivery_failures=$delivery_failures"
  printf 'Webetu notify completed: processed=%s skipped=%s blocked=%s reservation_failures=%s delivery_failures=%s\n' "$processed" "$skipped" "$blocked" "$reservation_failures" "$delivery_failures"

  if [ "$reservation_failures" -gt 0 ] || [ "$blocked" -gt 0 ] || [ "$delivery_failures" -gt 0 ]; then
    send_admin_alert "Run issue: reservation_failures=$reservation_failures blocked=$blocked delivery_failures=$delivery_failures. Normal reservation results were not sent to admin." || true
    return 1
  fi
}

main "$@"
