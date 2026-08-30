"""Provision model auth needed by Job Scout's OpenClaw agent wrapper."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import stat
import time


PROVIDER = "vercel-ai-gateway"
KEY_ENV = "AI_GATEWAY_API_KEY"


class ModelAuthError(RuntimeError):
  """Raised when model auth cannot be reconciled."""


def _parse_env_file(path: Path) -> dict[str, str]:
  values: dict[str, str] = {}
  if not path.is_file():
    return values
  for raw_line in path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
      continue
    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
      value = value[1:-1]
    if key:
      values[key] = value
  return values


def openclaw_dir_from_env(env: dict[str, str] | None = None) -> Path:
  env = env or os.environ
  configured = (env.get("JOB_SCOUT_OPENCLAW_DIR") or "").strip()
  if configured:
    return Path(configured).expanduser()
  home = (env.get("OPENCLAW_HOME") or "").strip()
  if home:
    path = Path(home).expanduser()
    return path if path.name == ".openclaw" else path / ".openclaw"
  return Path("/home/joseph/.openclaw")


def model_api_key(openclaw_dir: Path, env: dict[str, str] | None = None) -> str:
  env = env or os.environ
  direct = (env.get(KEY_ENV) or "").strip()
  if direct:
    return direct
  env_path = Path(env.get("OPENCLAW_ENV_PATH") or openclaw_dir / ".env").expanduser()
  return (_parse_env_file(env_path).get(KEY_ENV) or "").strip()


def _load_profiles(database: sqlite3.Connection) -> dict:
  database.execute(
    "CREATE TABLE IF NOT EXISTS auth_profile_store ("
    "store_key TEXT PRIMARY KEY, store_json TEXT NOT NULL, updated_at INTEGER NOT NULL)"
  )
  row = database.execute(
    "SELECT store_json FROM auth_profile_store WHERE store_key = 'primary'"
  ).fetchone()
  try:
    payload = json.loads(row[0]) if row else {"version": 1, "profiles": {}}
  except (TypeError, json.JSONDecodeError):
    payload = {"version": 1, "profiles": {}}
  if not isinstance(payload, dict):
    payload = {"version": 1, "profiles": {}}
  payload.setdefault("version", 1)
  profiles = payload.get("profiles")
  if not isinstance(profiles, dict):
    payload["profiles"] = {}
  return payload


def provision_agent_auth(agent_id: str, openclaw_dir: Path, key: str) -> Path:
  if not agent_id:
    raise ModelAuthError("agent id is required")
  if not key:
    raise ModelAuthError(f"{KEY_ENV} is unavailable for Job Scout model auth")
  agent_dir = openclaw_dir / "agents" / agent_id / "agent"
  agent_dir.mkdir(parents=True, exist_ok=True)
  database_path = agent_dir / "openclaw-agent.sqlite"
  database = sqlite3.connect(database_path)
  try:
    try:
      database.execute("UPDATE schema_meta SET agent_id = ? WHERE role = 'agent'", (agent_id,))
    except sqlite3.Error:
      pass
    payload = _load_profiles(database)
    payload["profiles"][f"{PROVIDER}:{agent_id}-env"] = {
      "type": "api_key",
      "provider": PROVIDER,
      "key": key,
    }
    database.execute(
      "INSERT INTO auth_profile_store(store_key, store_json, updated_at) "
      "VALUES ('primary', ?, ?) "
      "ON CONFLICT(store_key) DO UPDATE SET store_json=excluded.store_json, updated_at=excluded.updated_at",
      (json.dumps(payload, separators=(",", ":")), int(time.time() * 1000)),
    )
    database.commit()
  finally:
    database.close()
  database_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
  return database_path


def reconcile(agent_id: str = "main", env: dict[str, str] | None = None) -> Path:
  env = env or os.environ
  openclaw_dir = openclaw_dir_from_env(env)
  key = model_api_key(openclaw_dir, env)
  return provision_agent_auth(agent_id, openclaw_dir, key)


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description="reconcile Job Scout OpenClaw model auth")
  parser.add_argument("--agent", default="main")
  parser.add_argument("--json", action="store_true")
  args = parser.parse_args(argv)
  path = reconcile(args.agent)
  if args.json:
    print(json.dumps({"ok": True, "agent": args.agent, "authStatePath": str(path)}))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
