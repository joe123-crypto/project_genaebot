from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class JobScoutDeploymentTests(unittest.TestCase):
  def test_job_scout_onboarding_is_enabled(self) -> None:
    config = json.loads((ROOT / "config/openclaw.template.json").read_text(encoding="utf-8"))

    plugin = config["plugins"]["entries"]["public-service-gate"]["config"]
    self.assertIs(plugin["jobScoutOnboardingEnabled"], True)

  def test_browser_is_optional_and_does_not_gate_openclaw(self) -> None:
    compose = (ROOT / "deploy/docker-compose.override.yml").read_text(encoding="utf-8")

    self.assertNotIn("depends_on:", compose)
    self.assertIn("profiles:\n      - job-scout", compose)
    self.assertNotIn("seccomp=", compose)
    self.assertNotIn("no-new-privileges", compose)
    self.assertIn("user: ubuntu", compose)
    self.assertIn("/tmp:size=256m,uid=1000,gid=1000,mode=1770", compose)
    self.assertIn("JOB_SCOUT_BROWSER_TOKEN: ${JOB_SCOUT_BROWSER_TOKEN:-}", compose)
    self.assertNotIn("JOB_SCOUT_PILOT_USER_IDS", compose)
    self.assertEqual(compose.count("JOB_SCOUT_BROWSER_TOKEN: ${JOB_SCOUT_BROWSER_TOKEN:-}"), 2)

  def test_scheduler_has_no_pilot_allowlist(self) -> None:
    installer = (ROOT / "deploy/install-job-scout.sh").read_text(encoding="utf-8")

    self.assertIn('NAME="job-scout"', installer)
    self.assertIn('LEGACY_NAME="job-scout-pilot"', installer)
    self.assertNotIn("--pilot-only", installer)
    self.assertNotIn("JOB_SCOUT_PILOT_USER_IDS", installer)
    self.assertIn('"--limit-ready","100"', installer)

  def test_healthcheck_exercises_chromium_readiness(self) -> None:
    compose = (ROOT / "deploy/docker-compose.override.yml").read_text(encoding="utf-8")
    server = (ROOT / "workspace/job-scout/web_apply_server.mjs").read_text(encoding="utf-8")

    self.assertIn("/ready", compose)
    self.assertIn("probeChromium(chromium)", server)

  def test_optional_worker_starts_after_core_restart(self) -> None:
    deploy = (ROOT / "deploy/openclaw-deploy.sh").read_text(encoding="utf-8")
    restart = deploy.index("  restart_and_check\n", deploy.index("main()"))
    optional = deploy.index("  start_optional_job_scout_browser\n", deploy.index("main()"))

    self.assertLess(restart, optional)
    self.assertIn("core gateway remains available", deploy)

  def test_live_source_bind_mount_is_refreshed_safely(self) -> None:
    deploy = (ROOT / "deploy/openclaw-deploy.sh").read_text(encoding="utf-8")
    stage = deploy[deploy.index("stage_source()") : deploy.index("write_generated_marker()")]
    recreate = deploy.index("  recreate_boundary_stack\n", deploy.index("main()"))
    ensure_running = deploy.index("  ensure_container_running\n", deploy.index("main()"))

    self.assertNotIn('rm -rf "$STAGE_DIR"', stage)
    self.assertIn('mkdir -p "$STAGE_DIR/workspace"', stage)
    self.assertLess(recreate, ensure_running)

  def test_container_service_waits_for_docker_api(self) -> None:
    unit = (ROOT / "deploy/systemd/openclaw-gateway-container.service").read_text(encoding="utf-8")
    deploy = (ROOT / "deploy/openclaw-deploy.sh").read_text(encoding="utf-8")

    self.assertIn("ExecStartPre=/usr/local/libexec/openclaw-wait-for-docker", unit)
    self.assertIn("install_container_service", deploy)
    self.assertIn('run_root install -D -m 0644 "$SERVICE_UNIT_SRC" "$SERVICE_UNIT_DST"', deploy)

  def test_docker_readiness_check_succeeds_and_is_bounded(self) -> None:
    waiter = ROOT / "deploy/systemd/wait-for-docker.sh"
    with tempfile.TemporaryDirectory() as tmp:
      fake = Path(tmp) / "docker"
      fake.write_text("#!/bin/sh\n[ \"$1\" = info ]\n", encoding="utf-8")
      fake.chmod(0o755)
      env = {
        **os.environ,
        "DOCKER_BIN": str(fake),
        "DOCKER_READY_ATTEMPTS": "1",
        "DOCKER_READY_INTERVAL_SECONDS": "0",
      }
      success = subprocess.run(["bash", str(waiter)], env=env, capture_output=True, text=True, check=False)
      fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
      failure = subprocess.run(["bash", str(waiter)], env=env, capture_output=True, text=True, check=False)

    self.assertEqual(success.returncode, 0, success.stderr)
    self.assertEqual(failure.returncode, 1)
    self.assertIn("did not become ready after 1 attempts", failure.stderr)


if __name__ == "__main__":
  unittest.main()
