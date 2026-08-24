from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import sqlite3
import stat
import tempfile
import unittest

from jobscout import model_auth


def load_profiles(openclaw_dir: Path, agent: str = "main") -> dict:
  database_path = openclaw_dir / "agents" / agent / "agent" / "openclaw-agent.sqlite"
  database = sqlite3.connect(database_path)
  try:
    row = database.execute(
      "SELECT store_json FROM auth_profile_store WHERE store_key = 'primary'"
    ).fetchone()
  finally:
    database.close()
  return json.loads(row[0])


class ModelAuthTests(unittest.TestCase):
  def test_reconcile_creates_main_profile_from_environment(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      openclaw_dir = Path(tmp) / ".openclaw"
      path = model_auth.reconcile(env={
        "JOB_SCOUT_OPENCLAW_DIR": str(openclaw_dir),
        "AI_GATEWAY_API_KEY": "secret-key",
      })

      payload = load_profiles(openclaw_dir)
      profile = payload["profiles"]["vercel-ai-gateway:main-env"]
      self.assertEqual(profile["provider"], "vercel-ai-gateway")
      self.assertEqual(profile["type"], "api_key")
      self.assertEqual(profile["key"], "secret-key")
      self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

  def test_reconcile_reads_key_from_env_file_and_preserves_profiles(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      openclaw_dir = Path(tmp) / ".openclaw"
      env_path = Path(tmp) / "openclaw.env"
      env_path.write_text("AI_GATEWAY_API_KEY='file-key'\n", encoding="utf-8")
      database_dir = openclaw_dir / "agents" / "main" / "agent"
      database_dir.mkdir(parents=True)
      database = sqlite3.connect(database_dir / "openclaw-agent.sqlite")
      try:
        database.execute(
          "CREATE TABLE auth_profile_store ("
          "store_key TEXT PRIMARY KEY, store_json TEXT NOT NULL, updated_at INTEGER NOT NULL)"
        )
        database.execute(
          "INSERT INTO auth_profile_store VALUES ('primary', ?, 1)",
          (json.dumps({"version": 1, "profiles": {
            "other:kept": {"type": "api_key", "provider": "other", "key": "kept"}
          }}),),
        )
        database.commit()
      finally:
        database.close()

      model_auth.reconcile(env={
        "JOB_SCOUT_OPENCLAW_DIR": str(openclaw_dir),
        "OPENCLAW_ENV_PATH": str(env_path),
      })

      profiles = load_profiles(openclaw_dir)["profiles"]
      self.assertEqual(profiles["other:kept"]["key"], "kept")
      self.assertEqual(profiles["vercel-ai-gateway:main-env"]["key"], "file-key")

  def test_reconcile_fails_without_key(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      with self.assertRaisesRegex(model_auth.ModelAuthError, "AI_GATEWAY_API_KEY"):
        model_auth.reconcile(env={"JOB_SCOUT_OPENCLAW_DIR": str(Path(tmp) / ".openclaw")})

  def test_cli_json_does_not_print_secret(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      result = subprocess.run(
        [
          sys.executable,
          "-m",
          "jobscout.model_auth",
          "--agent",
          "main",
          "--json",
        ],
        check=False,
        capture_output=True,
        env={
          "JOB_SCOUT_OPENCLAW_DIR": str(Path(tmp) / ".openclaw"),
          "AI_GATEWAY_API_KEY": "secret-key",
          "PYTHONPATH": str(Path(__file__).resolve().parent),
        },
        text=True,
      )

      self.assertEqual(result.returncode, 0, result.stderr)
      self.assertNotIn("secret-key", result.stdout)
      payload = json.loads(result.stdout)
      self.assertEqual(payload["agent"], "main")


if __name__ == "__main__":
  unittest.main()
