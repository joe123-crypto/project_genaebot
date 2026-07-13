"""Outbound delivery for Job Scout: email, WhatsApp, and the web-apply worker.

Wraps the genaie-mail and openclaw CLIs (via a JSON subprocess helper) and the
website-application worker HTTP call. Imported by dispatch orchestration.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.request

from genaie_env import env_default

from jobscout import api
from .constants import DEFAULT_WEB_APPLY_URL, GENAIE_MAIL_BIN
from .profile import subscriber_user_id
from .subscriber import display_name, subscriber_cv_file, subscriber_sender_email
from .textutil import clean_job_title, natural_email_text, public_text


def parse_json_output(output: str) -> dict:
  try:
    data = json.loads(output or "{}")
  except json.JSONDecodeError:
    return {}
  return data if isinstance(data, dict) else {}


def token_store_key_for_uid(uid: str) -> str:
  digest = hashlib.sha256(str(uid or "").encode("utf-8")).digest()
  encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
  return f"firebase:{encoded}"


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


def gmail_sender_connected(subscriber: dict) -> bool:
  result = run_json_command([str(GENAIE_MAIL_BIN), "senders"], timeout=45)
  senders = result.get("senders")
  if not isinstance(senders, dict):
    return False
  return token_store_key_for_uid(subscriber_user_id(subscriber)) in senders


def send_application_email(subscriber: dict, candidate: dict, cover_letter: Path) -> dict:
  email = str(candidate.get("applicationEmail") or "").strip()
  if not email:
    raise RuntimeError("candidate is missing applicationEmail")
  role = clean_job_title(candidate.get("role")) or "advertised role"
  subject = natural_email_text(f"Application for {role} - {display_name(subscriber)}", 180)
  return run_json_command([
    str(GENAIE_MAIL_BIN),
    "send",
    "--from",
    subscriber_sender_email(subscriber),
    "--sender-uid",
    subscriber_user_id(subscriber),
    "--to",
    email,
    "--subject",
    subject,
    "--body-file",
    str(cover_letter),
    "--attach",
    subscriber_cv_file(subscriber),
    "--force",
  ])


def send_whatsapp_message(target: str, message: str) -> dict:
  command_env = os.environ.copy()
  command_env.pop("OPENCLAW_HOME", None)
  gateway_token = command_env.get("GATEWAY_AUTH_TOKEN", "").strip()
  if gateway_token:
    command_env["OPENCLAW_GATEWAY_TOKEN"] = gateway_token
  return run_json_command([
    str(api.OPENCLAW_BIN),
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


def send_whatsapp_summary(phone: str, message: str) -> dict:
  return send_whatsapp_message(phone, message)


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


def request_web_application(payload: dict, timeout: int = 180) -> dict:
  url = env_default("JOB_SCOUT_WEB_APPLY_URL", DEFAULT_WEB_APPLY_URL).strip()
  token = env_default("JOB_SCOUT_BROWSER_TOKEN", "").strip()
  if not url:
    raise RuntimeError("website application worker is not configured")
  if not token:
    raise RuntimeError("website application worker token is not configured")
  request = urllib.request.Request(
    url,
    data=json.dumps(payload).encode("utf-8"),
    method="POST",
    headers={
      "Content-Type": "application/json",
      "Accept": "application/json",
      "Authorization": f"Bearer {token}",
    },
  )
  try:
    with urllib.request.urlopen(request, timeout=timeout) as response:
      data = json.loads(response.read().decode("utf-8") or "{}")
  except urllib.error.HTTPError as err:
    raise RuntimeError(f"website application worker returned HTTP {err.code}") from err
  except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as err:
    raise RuntimeError("website application worker is unavailable") from err
  if not isinstance(data, dict) or data.get("outcome") not in {"submitted", "action_required", "failed"}:
    raise RuntimeError("website application worker returned an invalid result")
  return data
