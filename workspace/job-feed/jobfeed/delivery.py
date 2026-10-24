"""WhatsApp delivery for the job feed, through the OpenClaw CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

from .config import OPENCLAW_BIN_FALLBACKS
from .textutil import public_text


def resolve_openclaw_bin(fallbacks: tuple[Path, ...] = OPENCLAW_BIN_FALLBACKS) -> Path:
  configured = os.environ.get("OPENCLAW_BIN", "").strip()
  if configured:
    return Path(configured)
  discovered = shutil.which("openclaw")
  if discovered:
    return Path(discovered)
  for candidate in fallbacks:
    if candidate.is_file() and os.access(candidate, os.X_OK):
      return candidate
  return Path("openclaw")


OPENCLAW_BIN = resolve_openclaw_bin()


def parse_json_output(output: str) -> dict:
  try:
    data = json.loads(output or "{}")
  except json.JSONDecodeError:
    return {}
  return data if isinstance(data, dict) else {}


def run_json_command(command: list[str], timeout: int = 90, env: dict[str, str] | None = None) -> dict:
  try:
    completed = subprocess.run(
      command,
      check=False,
      capture_output=True,
      text=True,
      timeout=timeout,
      env=env,
    )
  except (OSError, subprocess.TimeoutExpired) as err:
    raise RuntimeError(public_text(err, 240)) from err
  if completed.returncode != 0:
    detail = completed.stderr or completed.stdout or f"exit {completed.returncode}"
    raise RuntimeError(public_text(detail, 240))
  return parse_json_output(completed.stdout)


def send_whatsapp_message(target: str, message: str) -> dict:
  command_env = os.environ.copy()
  command_env.pop("OPENCLAW_HOME", None)
  gateway_token = command_env.get("GATEWAY_AUTH_TOKEN", "").strip()
  if gateway_token:
    command_env["OPENCLAW_GATEWAY_TOKEN"] = gateway_token
  return run_json_command([
    str(OPENCLAW_BIN),
    "message",
    "send",
    "--channel",
    "whatsapp",
    "--target",
    target,
    "--message",
    message,
    "--json",
  ], env=command_env)
