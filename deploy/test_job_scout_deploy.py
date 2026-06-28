from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class JobScoutDeploymentTests(unittest.TestCase):
  def test_browser_is_optional_and_does_not_gate_openclaw(self) -> None:
    compose = (ROOT / "deploy/docker-compose.override.yml").read_text(encoding="utf-8")

    self.assertNotIn("depends_on:", compose)
    self.assertIn("profiles:\n      - job-scout", compose)
    self.assertNotIn("seccomp=", compose)
    self.assertNotIn("no-new-privileges", compose)
    self.assertIn("/tmp:size=256m,uid=1001,gid=1001,mode=1770", compose)
    self.assertIn("JOB_SCOUT_BROWSER_TOKEN: ${JOB_SCOUT_BROWSER_TOKEN:-}", compose)
    self.assertIn("JOB_SCOUT_PILOT_USER_IDS: ${JOB_SCOUT_PILOT_USER_IDS:-}", compose)
    self.assertEqual(compose.count("JOB_SCOUT_BROWSER_TOKEN: ${JOB_SCOUT_BROWSER_TOKEN:-}"), 2)

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


if __name__ == "__main__":
  unittest.main()
