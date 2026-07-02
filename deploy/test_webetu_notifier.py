import json
import os
import pathlib
import subprocess
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
NOTIFIER = ROOT / "workspace/cron/webetu-meals/notify-reservations.sh"
JOURNAL = ROOT / "workspace/cron/webetu-meals/delivery_journal.py"


class WebetuNotifierRetryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tempdir.name)
        self.state = self.base / "state"
        self.state.mkdir()
        self.config = self.base / "openclaw.json"
        self.config.write_text(
            json.dumps({"channels": {"whatsapp": {"allowFrom": ["*"]}}}),
            encoding="utf-8",
        )
        self.worker_config = self.base / "worker.json"
        self.worker_config.write_text("{}\n", encoding="utf-8")
        self.worker = self._write_executable(
            "fake-worker",
            r"""
            #!/usr/bin/env bash
            set -eu

            increment() {
              file="$TEST_STATE_DIR/$1.count"
              count=0
              if [ -f "$file" ]; then
                count="$(tr -d '\n' <"$file")"
              fi
              count=$((count + 1))
              printf '%s\n' "$count" >"$file"
              printf '%s\n' "$count"
            }

            command_name=""
            for arg in "$@"; do
              case "$arg" in
                list-registrations|reserve)
                  command_name="$arg"
                  break
                  ;;
              esac
            done

            if [ "$command_name" = "list-registrations" ]; then
              attempt="$(increment registrations)"
              if [ "$TEST_MODE" = "registration_flaky" ] && [ "$attempt" -eq 1 ]; then
                printf '%s\n' '{"status":"error","message":"Temporary failure in name resolution"}' >&2
                exit 1
              fi
              if [ "$TEST_MODE" = "registration_exhausted" ]; then
                printf '%s\n' '{"status":"error","message":"Service unavailable"}' >&2
                exit 1
              fi
              printf '%s\n' '{"status":"ok","registrations":[{"source":"firestore","user_id":"test-user","phone":"+213555000111","phone_hash":"testhash0001","registration_status":"active"}],"count":1}'
              exit 0
            fi

            if [ "$command_name" = "reserve" ]; then
              attempt="$(increment reservations)"
              if [ "$TEST_MODE" = "reservation_transient" ] && [ "$attempt" -eq 1 ]; then
                printf '%s\n' '{"status":"error","message":"Connection timed out"}' >&2
                exit 1
              fi
              if [ "$TEST_MODE" = "reservation_permanent" ]; then
                printf '%s\n' '{"status":"error","message":"Invalid Webetu credentials"}' >&2
                exit 1
              fi
              if [ "$TEST_MODE" = "reservation_transient" ]; then
                printf '%s\n' '{"user_id":"test-user","date":"2026-07-01","status":"already_booked","meals_reserved":3,"message":"Already booked","restaurant":"Test Residence"}'
              else
                printf '%s\n' '{"user_id":"test-user","date":"2026-07-01","status":"success","meals_reserved":3,"message":"Reserved","restaurant":"Test Residence"}'
              fi
              exit 0
            fi

            printf 'Unexpected worker arguments: %s\n' "$*" >&2
            exit 2
            """,
        )
        self.openclaw = self._write_executable(
            "fake-openclaw",
            r"""
            #!/usr/bin/env bash
            set -eu

            if [ "${1:-}" = "channels" ] && [ "${2:-}" = "status" ]; then
              if [ "$TEST_MODE" = "preflight_down" ]; then
                printf '%s\n' '{"channelAccounts":{"whatsapp":[{"accountId":"default","linked":false,"running":false,"connected":false}]}}'
              else
                printf '%s\n' '{"channelAccounts":{"whatsapp":[{"accountId":"default","linked":true,"running":true,"connected":true}]}}'
              fi
              exit 0
            fi

            if [ "${1:-}" != "message" ] || [ "${2:-}" != "send" ]; then
              printf 'Unexpected OpenClaw arguments: %s\n' "$*" >&2
              exit 2
            fi

            file="$TEST_STATE_DIR/deliveries.count"
            count=0
            if [ -f "$file" ]; then
              count="$(tr -d '\n' <"$file")"
            fi
            printf '%s\n' "$((count + 1))" >"$file"
            if [ "$TEST_MODE" = "delivery_failure" ]; then
              printf '%s\n' 'OutboundDeliveryError: No active WhatsApp Web listener for +213555000111' >&2
              exit 1
            fi
            printf '%s\n' '{"status":"ok"}'
            """,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def _write_executable(self, name, body):
        path = self.base / name
        path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
        path.chmod(0o755)
        return path

    def _count(self, name):
        path = self.state / f"{name}.count"
        return int(path.read_text(encoding="utf-8").strip()) if path.exists() else 0

    def _run_args(self, mode, args):
        env = os.environ.copy()
        env.update(
            {
                "TEST_MODE": mode,
                "TEST_STATE_DIR": str(self.state),
                "WEBETU_WORKER_MODE": "direct",
                "WEBETU_WORKER_BIN": str(self.worker),
                "WEBETU_WORKER_CONFIG": str(self.worker_config),
                "WEBETU_NOTIFY_LOG_FILE": str(self.state / "notification.log"),
                "WEBETU_NOTIFY_STATE_DIR": str(self.state),
                "WEBETU_NOTIFY_LOCK_DIR": str(self.state / "notify.lock"),
                "WEBETU_NOTIFY_SKIP_WINDOWS_FILE": str(self.state / "skip-windows.tsv"),
                "WEBETU_FAILED_DELIVERY_DIR": str(self.state / "failed-deliveries"),
                "WEBETU_DELIVERY_JOURNAL_TOOL": str(JOURNAL),
                "WEBETU_NOTIFY_HEARTBEAT_SECONDS": "1",
                "WEBETU_WORKER_STEP_TIMEOUT_SECONDS": "3",
                "WEBETU_REGISTRATION_MAX_ATTEMPTS": "3",
                "WEBETU_RESERVATION_MAX_ATTEMPTS": "2",
                "WEBETU_FIRST_RETRY_DELAY_SECONDS": "0",
                "WEBETU_SECOND_RETRY_DELAY_SECONDS": "0",
                "OPENCLAW_CONFIG": str(self.config),
                "OPENCLAW_HOME": str(self.base / "openclaw-home"),
                "OPENCLAW_BIN": str(self.openclaw),
                "OPENCLAW_RUN_USER": "webetu-notifier-test-user-does-not-exist",
            }
        )
        return subprocess.run(
            ["bash", str(NOTIFIER), *args],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )

    def _run(self, mode):
        return self._run_args(mode, ["2026-07-01"])

    def _pending_deliveries(self):
        return sorted((self.state / "failed-deliveries").glob("*.json"))

    def _resolved_deliveries(self):
        return sorted((self.state / "failed-deliveries" / "resolved").glob("*.json"))

    def test_registration_lookup_retries_then_completes(self):
        completed = self._run("registration_flaky")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self._count("registrations"), 2)
        self.assertEqual(self._count("reservations"), 1)
        self.assertEqual(self._count("deliveries"), 1)
        self.assertIn("reason=lookup_failure", completed.stdout)

    def test_registration_lookup_exhausts_bounded_attempts(self):
        completed = self._run("registration_exhausted")

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self._count("registrations"), 3)
        self.assertEqual(self._count("reservations"), 0)
        self.assertEqual(self._count("deliveries"), 1)
        self.assertIn("worker retry exhausted step=list-registrations attempts=3", completed.stdout)

    def test_transient_reservation_failure_retries_idempotently(self):
        completed = self._run("reservation_transient")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self._count("reservations"), 2)
        self.assertEqual(self._count("deliveries"), 1)
        self.assertIn("reason=transient_failure", completed.stdout)
        self.assertIn("status=already_booked", completed.stdout)

    def test_permanent_reservation_failure_is_not_retried(self):
        completed = self._run("reservation_permanent")

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self._count("reservations"), 1)
        self.assertEqual(self._count("deliveries"), 2)
        self.assertIn("reason=permanent_failure", completed.stdout)

    def test_disconnected_preflight_aborts_before_worker_or_delivery(self):
        completed = self._run("preflight_down")

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self._count("registrations"), 0)
        self.assertEqual(self._count("reservations"), 0)
        self.assertEqual(self._count("deliveries"), 0)
        self.assertIn("action=abort_before_reservations", completed.stdout)

    def test_successful_delivery_is_archived_not_pending(self):
        completed = self._run("normal")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self._pending_deliveries(), [])
        self.assertEqual(len(self._resolved_deliveries()), 1)
        payload = json.loads(self._resolved_deliveries()[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "delivered")
        self.assertNotIn("phone", {key.lower() for key in payload})

    def test_failed_delivery_stays_pending_without_raw_phone(self):
        completed = self._run("delivery_failure")

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(len(self._pending_deliveries()), 1)
        record_text = self._pending_deliveries()[0].read_text(encoding="utf-8")
        self.assertNotIn("+213555000111", record_text)
        payload = json.loads(record_text)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["phoneHash"], "testhash0001")
        self.assertIn("Webetu meals", payload["message"])
        self.assertIn("[redacted-phone]", payload["lastError"])

    def test_list_failed_outputs_safe_metadata_only(self):
        failed = self._run("delivery_failure")
        self.assertEqual(failed.returncode, 1)
        delivery_id = json.loads(
            self._pending_deliveries()[0].read_text(encoding="utf-8")
        )["deliveryId"]

        listed = self._run_args("normal", ["--list-failed"])

        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn(delivery_id, listed.stdout)
        self.assertIn("testhash0001", listed.stdout)
        self.assertNotIn("+213555000111", listed.stdout)
        self.assertNotIn("Reservation results", listed.stdout)

    def test_targeted_retry_revalidates_and_archives_one_delivery(self):
        failed = self._run("delivery_failure")
        self.assertEqual(failed.returncode, 1)
        delivery_id = json.loads(
            self._pending_deliveries()[0].read_text(encoding="utf-8")
        )["deliveryId"]

        retried = self._run_args("normal", ["--retry-failed", delivery_id])

        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(self._pending_deliveries(), [])
        self.assertEqual(len(self._resolved_deliveries()), 1)
        self.assertIn("delivery retry ok", retried.stdout)


if __name__ == "__main__":
    unittest.main()
