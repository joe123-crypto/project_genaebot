from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
WRAPPERS = (
  "job-scout",
  "job-scout-agent",
  "job-scout-channel-health",
  "job-scout-convert-pending",
)
CLI_COMMANDS = {
  "build-cv",
  "check-runtime",
  "convert-pending-cvs",
  "count-approved-applications",
  "deliver-group-feed",
  "dispatch-jsearch",
  "fetch-candidates",
  "finish-dispatch",
  "list-applications",
  "list-ready",
  "reconcile-agent-state",
  "record-application",
  "record-dispatch-outcome",
  "repair-cv",
  "resolve-location",
  "save-profile",
  "stage-cv",
  "start-dispatch",
  "status",
  "summary",
  "verify-dispatch",
}


def write_executable(path: Path, source: str) -> None:
  path.write_text(source, encoding="utf-8")
  path.chmod(0o700)


class PortabilityTests(unittest.TestCase):
  def test_all_modules_import_from_an_unrelated_working_directory(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      temp_root = Path(tmp)
      env = os.environ.copy()
      env.update({
        "JOB_SCOUT_CHANNEL_DB": str(temp_root / "channel" / "posts.sqlite3"),
        "JOB_SCOUT_CHANNEL_HEALTH": str(temp_root / "channel" / "health.json"),
        "JOB_SCOUT_RUNTIME_ROOT": str(temp_root / "runtime"),
        "JOB_SCOUT_WORKSPACE_ROOT": str(temp_root / "workspace"),
        "OPENCLAW_ENV_PATH": str(temp_root / "missing.env"),
        "OPENCLAW_PENDING_LINKS_PATH": str(temp_root / "account-link" / "pending.json"),
        "PYTHONPATH": str(ROOT),
      })
      code = """
import argparse
import importlib
import json
import pkgutil
import job_scout
import jobscout
import scrapers
for package in (jobscout, scrapers):
  for module in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
    importlib.import_module(module.name)
subparsers = next(action for action in job_scout.build_parser()._actions if isinstance(action, argparse._SubParsersAction))
print(json.dumps({"commands": sorted(subparsers.choices)}))
"""
      result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=temp_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
      )
      self.assertEqual(set(json.loads(result.stdout)["commands"]), CLI_COMMANDS)
      self.assertEqual(list(temp_root.iterdir()), [])

  def test_sources_have_no_forbidden_sibling_imports_or_workspace_path_injection(self) -> None:
    source_paths = [ROOT / "job_scout.py", *sorted((ROOT / "jobscout").rglob("*.py"))]
    for path in source_paths:
      source = path.read_text(encoding="utf-8")
      with self.subTest(path=path.relative_to(ROOT)):
        self.assertNotIn("from genaie_env", source)
        self.assertNotIn("from pending_cache", source)
        self.assertNotIn("from dashboard_telemetry", source)
        self.assertNotIn("workspace/lib", source)
        self.assertNotIn("workspace/account-link", source)
        self.assertNotIn("sys.path.insert", source)

  def test_scraper_direct_execution_shims_only_add_the_local_repository_root(self) -> None:
    scraper_names = ("applynow.py", "iharare.py", "vacancybox.py", "vacancymail.py")
    with tempfile.TemporaryDirectory() as tmp:
      env = os.environ.copy()
      env.pop("PYTHONPATH", None)
      for name in scraper_names:
        path = ROOT / "scrapers" / name
        source = path.read_text(encoding="utf-8")
        with self.subTest(name=name):
          self.assertIn("if __package__ in (None, \"\"):", source)
          self.assertNotIn(".openclaw", source)
          result = subprocess.run(
            [sys.executable, str(path), "--help"],
            cwd=tmp,
            env=env,
            capture_output=True,
            text=True,
          )
          self.assertEqual(result.returncode, 0, result.stderr)

  def test_wrappers_remain_executable_and_job_scout_can_use_a_temp_workspace(self) -> None:
    for name in WRAPPERS:
      with self.subTest(name=name):
        self.assertTrue(stat.S_IMODE((ROOT / "bin" / name).stat().st_mode) & stat.S_IXUSR)

    with tempfile.TemporaryDirectory() as tmp:
      temp_root = Path(tmp)
      workspace = temp_root / "workspace"
      workspace.mkdir()
      (workspace / "job-scout").symlink_to(ROOT, target_is_directory=True)
      env = os.environ.copy()
      env.update({
        "JOB_SCOUT_RUNTIME_ROOT": str(temp_root / "runtime"),
        "JOB_SCOUT_WORKSPACE_ROOT": str(workspace),
        "OPENCLAW_ENV_PATH": str(temp_root / "missing.env"),
        "OPENCLAW_PENDING_LINKS_PATH": str(temp_root / "account-link" / "pending.json"),
      })
      result = subprocess.run(
        [str(ROOT / "bin" / "job-scout"), "--help"],
        cwd=temp_root,
        env=env,
        capture_output=True,
        text=True,
      )
      self.assertEqual(result.returncode, 0, result.stderr)
      self.assertIn("usage: job-scout", result.stdout)
      self.assertTrue((temp_root / "account-link").is_dir())

  def test_agent_wrapper_resolves_explicit_openclaw_before_path_then_uses_path(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      temp_root = Path(tmp)
      job_scout = temp_root / "job-scout"
      write_executable(job_scout, """#!/usr/bin/env bash
if [ "${1:-}" = dispatch-jsearch ]; then
  printf '%s\n' '{"ok":true,"agentPhase":"prepare","runId":"portable-run","claimCount":1,"busy":false}'
  exit 0
fi
if [ "${1:-}" = verify-dispatch ]; then
  exit 0
fi
printf 'unexpected job-scout command: %s\n' "$*" >&2
exit 90
""")
      path_bin = temp_root / "path-bin"
      path_bin.mkdir()
      path_openclaw = path_bin / "openclaw"
      explicit_openclaw = temp_root / "explicit-openclaw"
      recorder = """#!/usr/bin/env bash
printf '%s\n' "$*" > "$OPENCLAW_CAPTURE"
"""
      write_executable(path_openclaw, recorder)
      write_executable(explicit_openclaw, recorder)

      base_env = os.environ.copy()
      base_env.update({
        "AI_GATEWAY_API_KEY": "test-model-key",
        "GATEWAY_AUTH_TOKEN": "test-token",
        "JOB_SCOUT_BIN": str(job_scout),
        "JOB_SCOUT_OPENCLAW_DIR": str(temp_root / ".openclaw"),
        "JOB_SCOUT_RUNTIME_ROOT": str(temp_root / "runtime"),
        "JOB_SCOUT_WORKSPACE_ROOT": str(temp_root / "workspace"),
        "PATH": f"{path_bin}:{base_env['PATH']}",
      })

      explicit_capture = temp_root / "explicit.args"
      explicit_env = {**base_env, "OPENCLAW_BIN": str(explicit_openclaw), "OPENCLAW_CAPTURE": str(explicit_capture)}
      explicit_result = subprocess.run(
        [str(ROOT / "bin" / "job-scout-agent"), "--all", "--preview"],
        cwd=temp_root,
        env=explicit_env,
        capture_output=True,
        text=True,
      )
      self.assertEqual(explicit_result.returncode, 0, explicit_result.stderr)
      self.assertIn("agent --agent main", explicit_capture.read_text(encoding="utf-8"))

      path_capture = temp_root / "path.args"
      path_env = {**base_env, "OPENCLAW_CAPTURE": str(path_capture)}
      path_env.pop("OPENCLAW_BIN", None)
      path_result = subprocess.run(
        [str(ROOT / "bin" / "job-scout-agent"), "--all", "--preview"],
        cwd=temp_root,
        env=path_env,
        capture_output=True,
        text=True,
      )
      self.assertEqual(path_result.returncode, 0, path_result.stderr)
      self.assertIn("agent --agent main", path_capture.read_text(encoding="utf-8"))
      self.assertTrue(
        (temp_root / ".openclaw" / "agents" / "main" / "agent" / "openclaw-agent.sqlite").is_file()
      )

      source = (ROOT / "bin" / "job-scout-agent").read_text(encoding="utf-8")
      self.assertLess(source.index("command -v openclaw"), source.index("/usr/local/bin/openclaw"))
      self.assertLess(source.index("/usr/local/bin/openclaw"), source.index("/home/joseph/.npm-global/bin/openclaw"))
      self.assertLess(source.index("jobscout.model_auth --agent main"), source.index("agent --agent main"))


if __name__ == "__main__":
  unittest.main()
