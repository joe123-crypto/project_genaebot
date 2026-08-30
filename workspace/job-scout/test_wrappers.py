import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parent
# In the standalone checkout the wrappers sit in a sibling ``bin/`` directory.
# In the deployed workspace overlay the package lives under ``workspace/job-scout``
# while the wrappers are staged one level up in ``workspace/bin``.
BIN = ROOT / "bin"
if not BIN.is_dir():
  BIN = ROOT.parent / "bin"


class WrapperTests(unittest.TestCase):
  def run_wrapper(
    self,
    wrapper: Path,
    *args: str,
    env: dict[str, str] | None = None,
  ) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    if env:
      command_env.update(env)
    return subprocess.run(
      [str(wrapper), *args],
      check=False,
      capture_output=True,
      env=command_env,
      text=True,
    )

  def write_python_probe(self, path: Path) -> None:
    path.write_text(textwrap.dedent("""\
      import json
      from pathlib import Path
      import sys

      print(json.dumps({"args": sys.argv[1:], "source": str(Path(__file__).resolve())}))
    """), encoding="utf-8")

  def test_job_scout_prefers_standalone_checkout_layout(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      checkout = Path(directory)
      (checkout / "bin").mkdir()
      wrapper = checkout / "bin" / "job-scout"
      shutil.copy2(BIN / "job-scout", wrapper)
      self.write_python_probe(checkout / "job_scout.py")
      pending = checkout / "runtime" / "pending.json"

      result = self.run_wrapper(
        wrapper,
        "status",
        env={"OPENCLAW_PENDING_LINKS_PATH": str(pending)},
      )

      self.assertEqual(result.returncode, 0, result.stderr)
      payload = json.loads(result.stdout)
      self.assertEqual(payload["args"], ["status"])
      self.assertEqual(payload["source"], str((checkout / "job_scout.py").resolve()))
      self.assertTrue(pending.parent.is_dir())

  def test_job_scout_supports_deployed_workspace_layout(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory) / "workspace"
      (workspace / "bin").mkdir(parents=True)
      (workspace / "job-scout").mkdir()
      wrapper = workspace / "bin" / "job-scout"
      shutil.copy2(BIN / "job-scout", wrapper)
      self.write_python_probe(workspace / "job-scout" / "job_scout.py")

      result = self.run_wrapper(
        wrapper,
        "status",
        env={"OPENCLAW_PENDING_LINKS_PATH": str(workspace / "pending.json")},
      )

      self.assertEqual(result.returncode, 0, result.stderr)
      payload = json.loads(result.stdout)
      self.assertEqual(payload["source"], str((workspace / "job-scout" / "job_scout.py").resolve()))

  def test_channel_health_uses_current_checkout(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      checkout = Path(directory)
      (checkout / "bin").mkdir()
      wrapper = checkout / "bin" / "job-scout-channel-health"
      shutil.copy2(BIN / "job-scout-channel-health", wrapper)
      self.write_python_probe(checkout / "channel_health.py")

      result = self.run_wrapper(wrapper, "--json")

      self.assertEqual(result.returncode, 0, result.stderr)
      payload = json.loads(result.stdout)
      self.assertEqual(payload["args"], ["--json"])
      self.assertEqual(payload["source"], str((checkout / "channel_health.py").resolve()))

  def test_agent_and_converter_use_their_sibling_job_scout(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      checkout = Path(directory)
      bin_dir = checkout / "bin"
      bin_dir.mkdir()
      calls = checkout / "calls.log"
      fake_job_scout = bin_dir / "job-scout"
      fake_job_scout.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        printf '%s\\n' "$*" >> {str(calls)!r}
        if [ "${{1:-}}" = count-approved-applications ]; then
          printf '{{"approvedCount":0}}\\n'
        fi
      """), encoding="utf-8")
      fake_job_scout.chmod(0o755)
      agent = bin_dir / "job-scout-agent"
      converter = bin_dir / "job-scout-convert-pending"
      shutil.copy2(BIN / "job-scout-agent", agent)
      shutil.copy2(BIN / "job-scout-convert-pending", converter)

      agent_result = self.run_wrapper(agent, "--approved-live")
      converter_result = self.run_wrapper(
        converter,
        env={"JOB_SCOUT_RUNTIME_ROOT": str(checkout / "runtime")},
      )

      self.assertEqual(agent_result.returncode, 0, agent_result.stderr)
      self.assertEqual(converter_result.returncode, 0, converter_result.stderr)
      self.assertEqual(
        calls.read_text(encoding="utf-8").splitlines(),
        [
          "count-approved-applications --limit 100",
          "convert-pending-cvs --limit 100",
        ],
      )
  def test_agent_marks_run_failed_when_dispatch_turn_fails(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
      checkout = Path(directory)
      bin_dir = checkout / "bin"
      bin_dir.mkdir()
      (checkout / "skill").mkdir()
      (checkout / "skill" / "SKILL.md").write_text("skill", encoding="utf-8")
      calls = checkout / "calls.log"
      fake_job_scout = bin_dir / "job-scout"
      fake_job_scout.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        printf '%s\\n' "$*" >> {str(calls)!r}
        phase=""
        prev=""
        for a in "$@"; do
          [ "$prev" = "--agent-phase" ] && phase="$a"
          prev="$a"
        done
        if [ "${{1:-}}" = dispatch-jsearch ] && [ "$phase" = prepare ]; then
          printf '{{"ok":true,"agentPhase":"prepare","runId":"test-run","claimCount":1,"busy":false}}\\n'
        elif [ "${{1:-}}" = dispatch-jsearch ] && [ "$phase" = status ]; then
          printf '{{"claims":[{{"claimId":"c1","status":"pending"}}]}}\\n'
        fi
      """), encoding="utf-8")
      fake_job_scout.chmod(0o755)
      failing_openclaw = bin_dir / "fake-openclaw"
      failing_openclaw.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
      failing_openclaw.chmod(0o755)
      agent = bin_dir / "job-scout-agent"
      shutil.copy2(BIN / "job-scout-agent", agent)

      result = self.run_wrapper(
        agent,
        "--daily-live",
        env={
          "OPENCLAW_BIN": str(failing_openclaw),
          "GATEWAY_AUTH_TOKEN": "gateway-token",
          "AI_GATEWAY_API_KEY": "test-key",
          "JOB_SCOUT_OPENCLAW_DIR": str(checkout / "openclaw"),
          "PYTHONPATH": str(ROOT),
        },
      )

      self.assertEqual(result.returncode, 1, result.stderr)
      logged = calls.read_text(encoding="utf-8")
      # The failed agent turn must be cleaned up so the run does not stay frozen at
      # the prepared phase: fall back every non-terminal claim, then finish.
      self.assertIn("--agent-phase status --run-id test-run", logged)
      self.assertIn("--agent-phase fallback --run-id test-run --claim-id c1", logged)
      self.assertIn("--agent-phase finish --run-id test-run", logged)


if __name__ == "__main__":
  unittest.main()
