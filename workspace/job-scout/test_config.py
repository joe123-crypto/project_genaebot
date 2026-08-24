from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from jobscout import api, config
from jobscout.integrations import agent_genaie


ROOT = Path(__file__).resolve().parent
CONFIG_ENV_NAMES = (
  "AGENT_GENAI_BASE_URL",
  "AGENT_GENAI_INTERNAL_API_KEY",
  "AGENT_GENAI_MAIL_URL",
  "APP_BASE_URL",
  "GENAIE_MAIL_BIN",
  "JOB_SCOUT_CHANNEL_DB",
  "JOB_SCOUT_CHANNEL_HEALTH",
  "JOB_SCOUT_RUNTIME_ROOT",
  "JOB_SCOUT_RENDER_PDF_URL",
  "JOB_SCOUT_WEB_APPLY_URL",
  "JOB_SCOUT_WORKSPACE_ROOT",
  "OPENCLAW_BIN",
  "OPENCLAW_ENV_PATH",
  "OPENCLAW_PENDING_LINKS_PATH",
)


class ConfigTests(unittest.TestCase):
  def _snapshot(self, overrides: dict[str, str] | None = None) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
      env = os.environ.copy()
      for name in CONFIG_ENV_NAMES:
        env.pop(name, None)
      env.update({
        "OPENCLAW_ENV_PATH": str(Path(tmp) / "missing.env"),
        "PYTHONPATH": str(ROOT),
        **(overrides or {}),
      })
      code = """
import json
from jobscout import config, constants
from jobscout.integrations import agent_genaie
print(json.dumps({
  "agentBaseUrl": agent_genaie.agent_base_url(),
  "channelDb": str(config.CHANNEL_DB_PATH),
  "channelHealth": str(config.CHANNEL_HEALTH_PATH),
  "dispatchRoot": str(constants.DEFAULT_DISPATCH_ROOT),
  "gmailBin": str(config.GENAIE_MAIL_BIN),
  "groupFeedRoot": str(constants.DEFAULT_GROUP_FEED_ROOT),
  "pendingLinks": str(config.PENDING_LINKS_PATH),
  "renderPdfUrl": config.render_pdf_url(),
  "runtimeRoot": str(config.RUNTIME_ROOT),
  "storageRoot": str(constants.DEFAULT_STORAGE_ROOT),
  "webApplyUrl": config.web_apply_url(),
  "workspaceRoot": str(config.WORKSPACE_ROOT),
}))
"""
      result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp,
        env=env,
        check=True,
        capture_output=True,
        text=True,
      )
      return json.loads(result.stdout)

  def test_exact_production_defaults(self) -> None:
    self.assertEqual(self._snapshot(), {
      "agentBaseUrl": "http://localhost:3000",
      "channelDb": "/home/joseph/.openclaw/runtime/job-scout-channel/posts.sqlite3",
      "channelHealth": "/home/joseph/.openclaw/runtime/job-scout-channel/health.json",
      "dispatchRoot": "/home/joseph/.openclaw/runtime/job-scout/dispatch-runs",
      "gmailBin": "/home/joseph/.openclaw/workspace/bin/genaie-mail",
      "groupFeedRoot": "/home/joseph/.openclaw/runtime/job-scout/group-feed",
      "pendingLinks": "/home/joseph/.openclaw/runtime/account-link/pending-links.json",
      "renderPdfUrl": "http://job-scout-browser:8787/render-pdf",
      "runtimeRoot": "/home/joseph/.openclaw/runtime/job-scout",
      "storageRoot": "/home/joseph/.openclaw/runtime/job-scout/users",
      "webApplyUrl": "http://job-scout-browser:8787/apply",
      "workspaceRoot": "/home/joseph/.openclaw/workspace",
    })

  def test_path_and_api_overrides(self) -> None:
    snapshot = self._snapshot({
      "AGENT_GENAI_BASE_URL": "https://agent.example.test/",
      "GENAIE_MAIL_BIN": "/tmp/tools/mail",
      "JOB_SCOUT_CHANNEL_DB": "/tmp/channel/posts.db",
      "JOB_SCOUT_CHANNEL_HEALTH": "/tmp/channel/health.json",
      "JOB_SCOUT_RUNTIME_ROOT": "/tmp/job-runtime",
      "JOB_SCOUT_RENDER_PDF_URL": "https://browser.example/render",
      "JOB_SCOUT_WEB_APPLY_URL": "https://browser.example/apply",
      "JOB_SCOUT_WORKSPACE_ROOT": "/tmp/workspace",
      "OPENCLAW_PENDING_LINKS_PATH": "/tmp/links/pending.json",
    })
    self.assertEqual(snapshot["agentBaseUrl"], "https://agent.example.test")
    self.assertEqual(snapshot["workspaceRoot"], "/tmp/workspace")
    self.assertEqual(snapshot["runtimeRoot"], "/tmp/job-runtime")
    self.assertEqual(snapshot["storageRoot"], "/tmp/job-runtime/users")
    self.assertEqual(snapshot["pendingLinks"], "/tmp/links/pending.json")
    self.assertEqual(snapshot["gmailBin"], "/tmp/tools/mail")
    self.assertEqual(snapshot["channelDb"], "/tmp/channel/posts.db")
    self.assertEqual(snapshot["channelHealth"], "/tmp/channel/health.json")
    self.assertEqual(snapshot["webApplyUrl"], "https://browser.example/apply")
    self.assertEqual(snapshot["renderPdfUrl"], "https://browser.example/render")

  def test_render_url_derives_from_overridden_apply_url(self) -> None:
    snapshot = self._snapshot({"JOB_SCOUT_WEB_APPLY_URL": "https://browser.example/custom-apply"})
    self.assertEqual(snapshot["webApplyUrl"], "https://browser.example/custom-apply")
    self.assertEqual(snapshot["renderPdfUrl"], "https://browser.example/render-pdf")

  def test_gmail_path_derives_from_overridden_workspace(self) -> None:
    snapshot = self._snapshot({"JOB_SCOUT_WORKSPACE_ROOT": "/tmp/portable-workspace"})
    self.assertEqual(snapshot["gmailBin"], "/tmp/portable-workspace/bin/genaie-mail")

  def test_pending_links_api_fallback_crosses_config_boundary(self) -> None:
    configured = Path("/tmp/configured-pending-links.json")
    with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(api, "PENDING_LINKS_PATH", configured):
      self.assertEqual(api.pending_links_path(), configured)
    with mock.patch.dict(os.environ, {"OPENCLAW_PENDING_LINKS_PATH": "/tmp/overridden-links.json"}, clear=True), \
         mock.patch.object(api, "PENDING_LINKS_PATH", configured):
      self.assertEqual(api.pending_links_path(), Path("/tmp/overridden-links.json"))

  def test_openclaw_resolution_uses_explicit_path_then_path_then_fallbacks(self) -> None:
    with mock.patch.dict(os.environ, {"OPENCLAW_BIN": "/tmp/explicit-openclaw"}, clear=True), \
         mock.patch.object(api.shutil, "which") as which:
      self.assertEqual(api.resolve_openclaw_bin(()), Path("/tmp/explicit-openclaw"))
      which.assert_not_called()

    with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
      api.shutil,
      "which",
      return_value="/tmp/path-openclaw",
    ):
      self.assertEqual(api.resolve_openclaw_bin(()), Path("/tmp/path-openclaw"))

    with tempfile.TemporaryDirectory() as tmp:
      fallback = Path(tmp) / "openclaw"
      fallback.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
      fallback.chmod(0o700)
      with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(api.shutil, "which", return_value=None):
        self.assertEqual(api.resolve_openclaw_bin((fallback,)), fallback)

  def test_agent_url_resolution_uses_process_then_env_file(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      env_path = Path(tmp) / ".env"
      env_path.write_text("APP_BASE_URL='https://from-file.example/'\n", encoding="utf-8")
      with mock.patch.object(config, "DEFAULT_ENV_PATH", env_path), mock.patch.dict(os.environ, {}, clear=True):
        self.assertEqual(agent_genaie.agent_base_url(), "https://from-file.example")
        self.assertEqual(agent_genaie.agent_base_url_source(), "APP_BASE_URL")
      with mock.patch.object(config, "DEFAULT_ENV_PATH", env_path), mock.patch.dict(
        os.environ,
        {"AGENT_GENAI_BASE_URL": "https://from-process.example/"},
        clear=True,
      ):
        self.assertEqual(agent_genaie.agent_base_url(), "https://from-process.example")
        self.assertEqual(agent_genaie.agent_base_url_source(), "AGENT_GENAI_BASE_URL")

  def test_personalization_is_disabled_by_default_and_env_toggles_it(self) -> None:
    with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
      config, "DEFAULT_ENV_PATH", Path("/nonexistent/.env")
    ):
      self.assertFalse(config.personalization_enabled())
    for value in ("1", "true", "yes", "on", "TRUE", "On"):
      with mock.patch.dict(os.environ, {"JOB_SCOUT_PERSONALIZATION_ENABLED": value}, clear=True):
        self.assertTrue(config.personalization_enabled(), value)
    for value in ("", "0", "false", "no", "off", "maybe"):
      with mock.patch.dict(os.environ, {"JOB_SCOUT_PERSONALIZATION_ENABLED": value}, clear=True), \
           mock.patch.object(config, "DEFAULT_ENV_PATH", Path("/nonexistent/.env")):
        self.assertFalse(config.personalization_enabled(), value)

  def test_mail_url_can_supply_the_agent_base(self) -> None:
    with mock.patch.dict(
      os.environ,
      {"AGENT_GENAI_MAIL_URL": "https://mail.example/internal/gmail/send"},
      clear=True,
    ):
      self.assertEqual(agent_genaie.agent_base_url(), "https://mail.example")
      self.assertEqual(agent_genaie.agent_base_url_source(), "AGENT_GENAI_MAIL_URL")


if __name__ == "__main__":
  unittest.main()
