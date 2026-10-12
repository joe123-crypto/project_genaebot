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

  def test_channel_listener_image_pins_baileys_and_runs_read_only(self) -> None:
    package = json.loads((ROOT / "workspace/job-scout/channel-listener/package.json").read_text(encoding="utf-8"))
    dockerfile = (ROOT / "deploy/job-scout-channel-listener.Dockerfile").read_text(encoding="utf-8")
    listener = (ROOT / "workspace/job-scout/channel-listener/listener.mjs").read_text(encoding="utf-8")
    self.assertEqual(package["dependencies"]["baileys"], "7.0.0-rc13")
    self.assertIn("FROM node:24-bookworm-slim", dockerfile)
    self.assertIn('CMD ["node", "listener.mjs"]', dockerfile)
    self.assertIn("fetchLatestBaileysVersion", listener)
    self.assertIn("version: versionResolution.version", listener)

  def test_scheduler_is_verified_command_wrapper(self) -> None:
    installer = (ROOT / "deploy/install-job-scout.sh").read_text(encoding="utf-8")

    self.assertIn('NAME="job-scout"', installer)
    self.assertIn('LEGACY_NAME="job-scout-pilot"', installer)
    self.assertNotIn("--pilot-only", installer)
    self.assertNotIn("JOB_SCOUT_PILOT_USER_IDS", installer)
    self.assertIn('CRON_EXPR="0 8 * * *"', installer)
    self.assertIn("--session isolated", installer)
    self.assertIn("--command-argv \"$COMMAND_ARGV\"", installer)
    self.assertIn("--command-cwd /home/joseph/.openclaw/workspace", installer)
    self.assertIn("--timeout-seconds 21600", installer)
    self.assertIn("--daily-live", installer)
    self.assertNotIn("sessions_yield", installer)
    self.assertNotIn("--message", installer)
    self.assertNotIn("--model", installer)

  def test_manual_agent_wrapper_prepares_before_starting_the_model(self) -> None:
    wrapper = (ROOT / "workspace/bin/job-scout-agent").read_text(encoding="utf-8")

    self.assertIn('prepare_json="$("${job_scout[@]}" "${prepare_args[@]}")"', wrapper)
    self.assertIn("read -r run_id claim_count busy", wrapper)
    self.assertIn("json.load(sys.stdin)", wrapper)
    self.assertNotIn("| jq", wrapper)
    self.assertIn("The trusted wrapper has already completed preparation", wrapper)
    self.assertIn("Do not run the prepare phase", wrapper)
    self.assertIn("--daily-live", wrapper)
    self.assertIn("--user-id", wrapper)
    self.assertIn("--match-threshold 51", wrapper)
    self.assertNotIn("--match-threshold 65", wrapper)
    self.assertIn("prepare_args+=(--write-group-feed-cache)", wrapper)
    self.assertIn('"${job_scout[@]}" verify-dispatch --run-id "$run_id" --require-no-failures', wrapper)
    self.assertNotIn("sleep 10", wrapper)
    self.assertNotIn("After filling the four slots, call sessions_yield", wrapper)
    self.assertIn("--agent-phase wait", (ROOT / "workspace/skills/job-scout/SKILL.md").read_text(encoding="utf-8"))
    self.assertLess(wrapper.index('prepare_json='), wrapper.index('"$openclaw_bin" agent'))

  def test_rerunning_the_installer_does_not_reenable_a_paused_schedule(self) -> None:
    installer = (ROOT / "deploy/install-job-scout.sh").read_text(encoding="utf-8")

    self.assertIn('INSTALL_STATE="preserve"', installer)
    self.assertIn('add_state=(--disabled)', installer)
    self.assertIn('.enabled == $expected_enabled', installer)

  def test_main_agent_subagents_inherit_default_model_with_narrow_tools(self) -> None:
    config = json.loads((ROOT / "config/openclaw.template.json").read_text(encoding="utf-8"))
    subagents = config["agents"]["defaults"]["subagents"]
    self.assertEqual(subagents["allowAgents"], ["main"])
    self.assertEqual(subagents["maxConcurrent"], 4)
    self.assertEqual(subagents["maxChildrenPerAgent"], 4)
    self.assertEqual(subagents["runTimeoutSeconds"], 2700)
    self.assertNotIn("model", subagents)
    self.assertEqual(config["tools"]["subagents"]["tools"]["allow"], ["exec", "process", "read", "edit", "write"])

  def test_job_scout_child_edits_prepopulated_candidate_ids(self) -> None:
    skill = (ROOT / "workspace/skills/job-scout/SKILL.md").read_text(encoding="utf-8")

    self.assertIn("already contains a private `agent-result.json` template", skill)
    self.assertIn("Never type, copy, reconstruct, or edit an opaque candidate ID", skill)
    self.assertIn("Use the `edit` tool", skill)
    self.assertIn("never add, remove, duplicate, or reorder assessments", skill)

  def test_group_feed_scheduler_is_separate_delivery_only_cron(self) -> None:
    installer = (ROOT / "deploy/install-job-scout-group-feed.sh").read_text(encoding="utf-8")
    main_installer = (ROOT / "deploy/install-job-scout.sh").read_text(encoding="utf-8")

    self.assertIn('NAME="job-scout-group-feed"', installer)
    self.assertIn('CRON_EXPR="20 9 * * *"', installer)
    self.assertIn('"deliver-group-feed"', installer)
    self.assertIn('"--refresh-if-stale"', installer)
    self.assertIn("JOB_SCOUT_GROUP_FEED_TARGET", installer)
    self.assertIn("directory groups list --channel whatsapp", installer)
    self.assertIn("@g.us", installer)
    self.assertNotIn("deliver-group-feed", main_installer)

  def test_hustlers_feed_scheduler_posts_one_job_per_day_from_shared_cache(self) -> None:
    installer = (ROOT / "deploy/install-job-scout-hustlers-feed.sh").read_text(encoding="utf-8")

    self.assertIn('NAME="job-scout-hustlers-feed"', installer)
    self.assertIn('CRON_EXPR="40 9 * * *"', installer)
    self.assertIn('"deliver-group-feed"', installer)
    self.assertIn('"--refresh-if-stale"', installer)
    self.assertIn('"--max-messages"', installer)
    # One job per day is the whole point of this cron.
    self.assertIn('MAX_MESSAGES="${JOB_SCOUT_HUSTLERS_FEED_MAX_MESSAGES:-1}"', installer)
    self.assertIn("JOB_SCOUT_HUSTLERS_FEED_TARGET", installer)
    self.assertIn("directory groups list --channel whatsapp", installer)
    self.assertIn("@g.us", installer)
    # The appended invite link defaults to the shared main jobs-group invite.
    self.assertIn('INVITE_URL="${JOB_SCOUT_HUSTLERS_FEED_INVITE_URL:-$MAIN_INVITE_URL}"', installer)
    self.assertIn('"--invite-url"', installer)

  def test_installer_adds_new_cron_before_removing_old_ones(self) -> None:
    installer = (ROOT / "deploy/install-job-scout.sh").read_text(encoding="utf-8")

    # A failed add must leave the previous schedule installed, so the old
    # jobs are snapshotted first, the new job added, and only then removed.
    self.assertLess(installer.index("cron list --all --json"), installer.index("cron add"))
    self.assertLess(installer.index("cron add"), installer.index("cron remove"))
    self.assertIn('[ "$old_id" != "$existing_id" ]', installer)

  def test_healthcheck_exercises_chromium_and_pdf_readiness(self) -> None:
    server = (ROOT / "workspace/job-scout/web_apply_server.mjs").read_text(encoding="utf-8")

    self.assertIn("probeChromium(chromium)", server)
    self.assertIn("probePdfRendering(chromium)", server)
    self.assertIn('request.url === "/render-pdf"', server)
    self.assertIn('"content-type": "application/pdf"', server)

  def test_gateway_image_includes_the_cv_converter_toolchain(self) -> None:
    dockerfile = (ROOT / "deploy/railway/Dockerfile").read_text(encoding="utf-8")

    self.assertIn("poppler-utils", dockerfile)
    self.assertIn("tesseract-ocr", dockerfile)

  def test_pending_cv_converter_runs_frequently_without_overlap(self) -> None:
    installer = (ROOT / "deploy/install-job-scout-cv-conversion.sh").read_text(encoding="utf-8")
    wrapper = (ROOT / "workspace/bin/job-scout-convert-pending").read_text(encoding="utf-8")

    self.assertIn('NAME="job-scout-cv-conversion"', installer)
    self.assertIn('CRON_EXPR="0 * * * * *"', installer)
    self.assertIn("--timeout-seconds 600", installer)
    self.assertIn('"/home/joseph/.openclaw/workspace/bin/job-scout-convert-pending"', installer)
    self.assertIn("flock -n 9", wrapper)
    self.assertIn("convert-pending-cvs --limit 100", wrapper)

  def test_approved_application_worker_runs_every_five_minutes(self) -> None:
    installer = (ROOT / "deploy/install-job-scout-approvals.sh").read_text(encoding="utf-8")
    wrapper = (ROOT / "workspace/bin/job-scout-agent").read_text(encoding="utf-8")

    self.assertIn('NAME="job-scout-approvals"', installer)
    self.assertIn('CRON_EXPR="0 */5 * * * *"', installer)
    self.assertIn('"--approved-live"', installer)
    self.assertIn("--approved-only --browser-apply", wrapper)

  def test_firestore_quota_defers_job_scout_schedule_without_hiding_other_failures(self) -> None:
    installer = (ROOT / "deploy/install-job-scout.sh").read_text(encoding="utf-8")

    self.assertIn("RESOURCE_EXHAUSTED|quota exceeded", installer)
    self.assertIn("exit 75", installer)
    self.assertLess(installer.index("check-runtime"), installer.index("cron list --all --json"))

    with tempfile.TemporaryDirectory() as temp_dir:
      temp = Path(temp_dir)
      fake_docker = temp / "docker"
      calls = temp / "calls.log"
      fake_docker.write_text(
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_DOCKER_CALLS"
if [ "${1:-}" = ps ]; then
  printf '%s\\n' browser-container
  exit 0
fi
if printf '%s\\n' "$*" | grep -q sha256sum; then
  printf '%s\\n' matching-token-hash
  exit 0
fi
if printf '%s\\n' "$*" | grep -q check-runtime; then
  if [ "${FAKE_RUNTIME_MODE:-quota}" = quota ]; then
    printf '%s\\n' '8 RESOURCE_EXHAUSTED: Quota exceeded.' >&2
    exit 1
  fi
  printf '%s\\n' 'runtime transport failed' >&2
  exit 42
fi
exit 0
""",
        encoding="utf-8",
      )
      fake_docker.chmod(0o755)
      env = {
        **os.environ,
        "OPENCLAW_EXEC_MODE": "docker",
        "DOCKER": str(fake_docker),
        "FAKE_DOCKER_CALLS": str(calls),
      }

      quota = subprocess.run(
        ["bash", str(ROOT / "deploy/install-job-scout.sh")],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
      )
      self.assertEqual(quota.returncode, 75, quota.stderr)
      self.assertIn("existing schedule was not changed", quota.stderr)
      self.assertNotIn("cron list", calls.read_text(encoding="utf-8"))

      env["FAKE_RUNTIME_MODE"] = "other"
      other = subprocess.run(
        ["bash", str(ROOT / "deploy/install-job-scout.sh")],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
      )
      self.assertEqual(other.returncode, 42, other.stderr)
      self.assertIn("runtime transport failed", other.stderr)
      self.assertNotIn("reconciliation was deferred", other.stderr)

  def test_browser_health_is_only_probed_where_a_browser_worker_can_exist(self) -> None:
    installer = (ROOT / "deploy/install-job-scout.sh").read_text(encoding="utf-8")

    # The browser worker is a separate container, so both the token-parity
    # preflight and the --check-browser probe belong inside the docker branch.
    # On Railway the gateway runs alone and the probe would always fail.
    docker_branch = installer.index('if [ "$GATEWAY_EXEC_MODE" = docker ]')
    else_branch = installer.index("\nelse\n", docker_branch)
    self.assertLess(docker_branch, installer.index("--check-browser"))
    self.assertLess(installer.index("--check-browser"), else_branch)
    # The subscriber/CV readiness gate is real on both runtimes.
    self.assertIn("--check-subscribers", installer)

  def test_railway_mode_installs_the_schedule_without_a_browser_worker(self) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
      temp = Path(temp_dir)
      fake_railway = temp / "railway"
      calls = temp / "calls.log"
      fake_railway.write_text(
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_RAILWAY_CALLS"
if printf '%s\\n' "$*" | grep -q check-runtime; then
  printf '%s\\n' '{"ok": true}'
  exit 0
fi
if printf '%s\\n' "$*" | grep -q 'cron list'; then
  printf '%s\\n' '{"jobs": []}'
  exit 0
fi
exit 0
""",
        encoding="utf-8",
      )
      fake_railway.chmod(0o755)
      env = {
        **os.environ,
        "OPENCLAW_EXEC_MODE": "railway",
        "RAILWAY": str(fake_railway),
        "FAKE_RAILWAY_CALLS": str(calls),
      }

      result = subprocess.run(
        ["bash", str(ROOT / "deploy/install-job-scout.sh")],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
      )
      recorded = calls.read_text(encoding="utf-8")

      # The readiness gate still runs, but never asks about the absent browser.
      self.assertIn("check-runtime", recorded)
      self.assertNotIn("--check-browser", recorded)
      self.assertIn("no browser worker on this runtime", result.stderr)
      # Having passed the gate, the installer must reach the cron work rather
      # than aborting on an unreachable worker.
      self.assertIn("cron list", recorded, result.stderr)

  def test_image_build_stages_the_source_overlay_and_verifies_it(self) -> None:
    dockerfile = (ROOT / "deploy/railway/Dockerfile").read_text(encoding="utf-8")
    staging = (ROOT / "deploy/stage-openclaw-source.sh").read_text(encoding="utf-8")

    # Staging must run before verification, so a bad overlay fails the build.
    self.assertLess(
      dockerfile.index("deploy/stage-openclaw-source.sh"),
      dockerfile.index("deploy/verify-staged-source.sh"),
    )
    self.assertNotIn('rm -rf "$stage_root"', staging)
    self.assertIn('"$stage_root/workspace"', staging)


if __name__ == "__main__":
  unittest.main()
