from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
RESOURCE_CHECK = ROOT / "deploy/check-deploy-resources.sh"
HEALTH_CHECK = ROOT / "deploy/ops/check-health.sh"
DEPLOY = ROOT / "deploy/openclaw-deploy.sh"


class ResourcePreflightTests(unittest.TestCase):
    def run_check(self, meminfo: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            meminfo_path = Path(tmp) / "meminfo"
            meminfo_path.write_text(meminfo, encoding="utf-8")
            return subprocess.run(
                ["bash", str(RESOURCE_CHECK)],
                env={**os.environ, "OPENCLAW_DEPLOY_MEMINFO_PATH": str(meminfo_path)},
                capture_output=True,
                text=True,
                check=False,
            )

    def test_sufficient_memory_and_swap_pass(self) -> None:
        completed = self.run_check(
            "MemAvailable: 2097152 kB\nSwapFree: 5242880 kB\nSwapTotal: 12582912 kB\n"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("CombinedHeadroom=7168 MiB", completed.stdout)
        self.assertIn("[OK]", completed.stdout)

    def test_low_memory_fails_before_deploy(self) -> None:
        completed = self.run_check(
            "MemAvailable: 524288 kB\nSwapFree: 102400 kB\nSwapTotal: 4194304 kB\n"
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("refusing deployment before production mutation", completed.stderr)


class HealthCheckTests(unittest.TestCase):
    def run_health(self, webetu_enabled: str, worker_mode: str = "healthy", status: str = "running") -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            calls = tmp_path / "calls.log"
            fake_docker = tmp_path / "docker"
            fake_docker.write_text(
                """#!/bin/sh
printf '%s\\n' "$*" >>"$FAKE_DOCKER_CALLS"
case "$1" in
  inspect)
    if [ "$2" = "-f" ]; then printf '%s\\n' "$FAKE_CONTAINER_STATUS"; fi
    exit 0
    ;;
  logs)
    printf '%s\\n' '[gateway] ready' 'Listening for WhatsApp inbound messages'
    exit 0
    ;;
  exec)
    case "$*" in
      *webetu-worker-safe*list-registrations*)
        case "$FAKE_WORKER_MODE" in
          healthy) printf '%s\\n' '{"status":"ok","count":0}'; exit 0 ;;
          quota) printf '%s\\n' 'google.api_core.exceptions.ResourceExhausted: 429 Quota exceeded.' >&2; exit 1 ;;
          failed) printf '%s\\n' 'worker unavailable' >&2; exit 1 ;;
        esac
        ;;
      *python3*) printf '%s\\n' '[OK] publicUsers count=0'; exit 0 ;;
      *) printf '%s\\n' '1M /home/joseph/.openclaw'; exit 0 ;;
    esac
    ;;
  system) printf '%s\\n' 'TYPE TOTAL ACTIVE SIZE RECLAIMABLE'; exit 0 ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            stop = threading.Event()

            def accept_connections() -> None:
                listener.settimeout(0.1)
                while not stop.is_set():
                    try:
                        connection, _ = listener.accept()
                    except TimeoutError:
                        continue
                    connection.close()

            thread = threading.Thread(target=accept_connections)
            thread.start()
            try:
                return subprocess.run(
                    ["bash", str(HEALTH_CHECK)],
                    env={
                        **os.environ,
                        "DOCKER": str(fake_docker),
                        "FAKE_DOCKER_CALLS": str(calls),
                        "FAKE_CONTAINER_STATUS": status,
                        "FAKE_WORKER_MODE": worker_mode,
                        "GATEWAY_PORT": str(port),
                        "OPENCLAW_WEBETU_ENABLED_OVERRIDE": webetu_enabled,
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
            finally:
                stop.set()
                thread.join()
                listener.close()

    def test_disabled_webetu_is_not_probed(self) -> None:
        completed = self.run_health("false")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("[SKIP] Webetu disabled", completed.stdout)
        self.assertNotIn("list-registrations", completed.stdout)

    def test_firestore_quota_is_a_concise_advisory(self) -> None:
        completed = self.run_health("true", worker_mode="quota")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("[WARN] Webetu Firestore quota is exhausted", completed.stdout)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)

    def test_core_container_failure_is_hard(self) -> None:
        completed = self.run_health("false", status="exited")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("[FAIL] container openclaw-gateway status is exited", completed.stdout)


class DeploymentContractTests(unittest.TestCase):
    def test_validated_sha_must_match_current_head(self) -> None:
        command = f'source "{DEPLOY}"; parse_args --validated-head definitely-wrong; syntax_checks'
        completed = subprocess.run(
            ["bash", "-c", command],
            env={**os.environ, "OPENCLAW_DIR": str(ROOT)},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("validated head does not match current source", completed.stderr)

    def test_wrapper_preflights_and_passes_validated_sha(self) -> None:
        wrapper = (ROOT / "deploy/github-actions/openclaw-prod-deploy").read_text(encoding="utf-8")
        self.assertIn('"$src/deploy/check-deploy-resources.sh"', wrapper)
        self.assertIn('--validated-head "$head_sha"', wrapper)
        self.assertIn('repo_user="joseph"', wrapper)
        self.assertIn('stat -c \'%u\' "$src"', wrapper)
        self.assertIn('runuser -u "$repo_user" -- git', wrapper)
        self.assertIn("export GIT_CONFIG_COUNT=1", wrapper)
        self.assertIn("export GIT_CONFIG_KEY_0=safe.directory", wrapper)
        self.assertIn('export GIT_CONFIG_VALUE_0="$src"', wrapper)
        self.assertNotIn("git config --global", wrapper)

    def test_deploy_is_single_restart_and_records_success_last(self) -> None:
        deploy = DEPLOY.read_text(encoding="utf-8")
        main = deploy[deploy.index("main()") :]
        self.assertNotIn("--force-recreate", deploy)
        self.assertLess(main.index("  restart_gateway_if_needed\n"), main.index("  wait_gateway_api_ready\n"))
        self.assertLess(main.index("  wait_gateway_api_ready\n"), main.index("  run_health_check\n"))
        self.assertLess(main.index("  start_optional_job_scout_channel_listener\n"), main.index("  record_successful_deploy\n"))
        self.assertIn("GATEWAY_RECREATED", deploy)

    def test_health_and_runner_controls_are_source_controlled(self) -> None:
        deploy = DEPLOY.read_text(encoding="utf-8")
        override = (ROOT / "deploy/systemd/actions-runner-openclaw-prod.override.conf").read_text(encoding="utf-8")
        self.assertIn('install -D -m 0755 "$HEALTH_CHECK_SRC" "$HEALTH_CHECK_DST"', deploy)
        for setting in ("Restart=on-failure", "RestartSec=10s", "OOMPolicy=kill", "KillMode=control-group"):
            self.assertIn(setting, override)


if __name__ == "__main__":
    unittest.main()
