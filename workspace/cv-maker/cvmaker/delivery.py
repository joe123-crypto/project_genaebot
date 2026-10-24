"""WhatsApp delivery for the CV maker.

One call: hand the finished PDF back to the individual who ordered it. The
OpenClaw send is the same two-argv delta from a plain text send that cron's ad
poster uses, `--media <path>`, so a CV arrives as a document rather than a wall
of text.

The caller is responsible for normalizing the target first (see build.py): a CV
carries the applicant's name, phone, email and address and must only ever go
back to the person who asked for it.
"""

from __future__ import annotations

import os
from pathlib import Path

import json
import subprocess

from .common import OPENCLAW_BIN, public_text


__all__ = ["result_message_id", "send_whatsapp_document", "send_whatsapp_message"]


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


def _cli_env() -> dict[str, str]:
  """OPENCLAW_HOME is dropped so the CLI resolves its own home, and a gateway
  token is forwarded under the name the CLI expects."""
  command_env = os.environ.copy()
  command_env.pop("OPENCLAW_HOME", None)
  gateway_token = command_env.get("GATEWAY_AUTH_TOKEN", "").strip()
  if gateway_token:
    command_env["OPENCLAW_GATEWAY_TOKEN"] = gateway_token
  return command_env


def send_whatsapp_message(target: str, message: str) -> dict:
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
  ], env=_cli_env())


def result_message_id(result: dict) -> str | None:
  for key in ("messageId", "id", "gmailMessageId"):
    value = str(result.get(key) or "").strip()
    if value:
      return value
  nested = result.get("message")
  if isinstance(nested, dict):
    for key in ("id", "messageId"):
      value = str(nested.get(key) or "").strip()
      if value:
        return value
  return None


def send_whatsapp_document(target: str, document_path: str | Path, message: str = "") -> dict:
  """Send a document (e.g. a rendered CV PDF) to a WhatsApp user as media.

  Same as send_whatsapp_message but attaches a file via --media.
  """
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
    "--media",
    str(document_path),
    "--json",
  ], env=_cli_env())
