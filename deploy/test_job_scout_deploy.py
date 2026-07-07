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

  def test_channel_listener_is_isolated_and_requires_pairing(self) -> None:
    compose = (ROOT / "deploy/docker-compose.override.yml").read_text(encoding="utf-8")
    deploy = (ROOT / "deploy/openclaw-deploy.sh").read_text(encoding="utf-8")
    pair = (ROOT / "deploy/pair-job-scout-channel.sh").read_text(encoding="utf-8")

    self.assertIn("- job-scout-channel", compose)
    self.assertIn("JOB_SCOUT_CHANNEL_BACKFILL_COUNT: \"10\"", compose)
    self.assertIn("JOB_SCOUT_CHANNELS_JSON:", compose)
    self.assertIn("0029VaYITFn2v1IsGm9zxP1k", compose)
    self.assertIn("0029VaQ9LFe8PgsLScuJES32", compose)
    self.assertIn("zim-job-vacancies-whatsapp", compose)
    self.assertIn("0029VacRRi9GE56qqFobOZ2K", compose)
    self.assertIn("everyday-vacancies-whatsapp", compose)
    self.assertIn("runtime/job-scout-channel:/data", compose)
    self.assertNotIn(".openclaw/credentials", compose)
    self.assertIn("cap_drop:\n      - ALL", compose)
    self.assertIn("auth/creds.json", deploy)
    self.assertIn("JOB_SCOUT_CHANNEL_PAIRING=1", pair)
    self.assertIn("install-job-scout-channel-health.sh", pair)

  def test_channel_listener_image_pins_baileys_and_runs_read_only(self) -> None:
    package = json.loads((ROOT / "workspace/job-scout/channel-listener/package.json").read_text(encoding="utf-8"))
    dockerfile = (ROOT / "deploy/job-scout-channel-listener.Dockerfile").read_text(encoding="utf-8")
    self.assertEqual(package["dependencies"]["baileys"], "7.0.0-rc13")
    self.assertIn("FROM node:24-bookworm-slim", dockerfile)
    self.assertIn('CMD ["node", "listener.mjs"]', dockerfile)

  def test_scheduler_has_no_pilot_allowlist(self) -> None:
    installer = (ROOT / "deploy/install-job-scout.sh").read_text(encoding="utf-8")

    self.assertIn('NAME="job-scout"', installer)
    self.assertIn('LEGACY_NAME="job-scout-pilot"', installer)
    self.assertNotIn("--pilot-only", installer)
    self.assertNotIn("JOB_SCOUT_PILOT_USER_IDS", installer)
    self.assertIn('"--limit-ready","100"', installer)
    self.assertIn('"--write-group-feed-cache"', installer)

  def test_group_feed_scheduler_is_separate_delivery_only_cron(self) -> None:
    installer = (ROOT / "deploy/install-job-scout-group-feed.sh").read_text(encoding="utf-8")
    main_installer = (ROOT / "deploy/install-job-scout.sh").read_text(encoding="utf-8")

    self.assertIn('NAME="job-scout-group-feed"', installer)
    self.assertIn('CRON_EXPR="20 9 * * *"', installer)
    self.assertIn('"deliver-group-feed"', installer)
    self.assertIn("JOB_SCOUT_GROUP_FEED_TARGET", installer)
    self.assertIn("directory groups list --channel whatsapp", installer)
    self.assertIn("@g.us", installer)
    self.assertNotIn("deliver-group-feed", main_installer)

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
    staging = (ROOT / "deploy/stage-openclaw-source.sh").read_text(encoding="utf-8")
    stage = deploy[deploy.index("stage_source()") : deploy.index("write_generated_marker()")]
    recreate = deploy.index("  recreate_boundary_stack\n", deploy.index("main()"))
    ensure_running = deploy.index("  ensure_container_running\n", deploy.index("main()"))

    self.assertIn('stage-openclaw-source.sh" "$OC_DIR" "$STAGE_DIR"', stage)
    self.assertNotIn('rm -rf "$stage_root"', staging)
    self.assertIn('"$stage_root/workspace"', staging)
    self.assertLess(recreate, ensure_running)

  def test_container_service_waits_for_docker_api(self) -> None:
    unit = (ROOT / "deploy/systemd/openclaw-gateway-container.service").read_text(encoding="utf-8")
    deploy = (ROOT / "deploy/openclaw-deploy.sh").read_text(encoding="utf-8")

    self.assertIn("ExecStartPre=/usr/local/libexec/openclaw-wait-for-docker", unit)
    self.assertIn("install_container_service", deploy)
    self.assertIn('run_root install -D -m 0644 "$SERVICE_UNIT_SRC" "$SERVICE_UNIT_DST"', deploy)

  def test_live_workspace_verification_survives_container_restart(self) -> None:
    deploy = (ROOT / "deploy/openclaw-deploy.sh").read_text(encoding="utf-8")
    verify = deploy[deploy.index("verify_live_workspace()") : deploy.index("main()")]

    self.assertIn('for _attempt in $(seq 1 5)', verify)
    self.assertIn('exec -i "$CONTAINER" python3', verify)
    self.assertIn("ensure_container_running", verify)

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
