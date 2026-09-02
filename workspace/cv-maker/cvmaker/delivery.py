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

from jobscout import api
from jobscout.delivery import result_message_id, run_json_command


__all__ = ["result_message_id", "send_whatsapp_document"]


def send_whatsapp_document(target: str, document_path: str | Path, message: str = "") -> dict:
  """Send a document (e.g. a rendered CV PDF) to a WhatsApp user as media.

  Mirrors jobscout.delivery.send_whatsapp_message but attaches a file via
  --media. OPENCLAW_HOME is dropped so the CLI resolves its own home, and a
  gateway token is forwarded under the name the CLI expects.
  """
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
    "--media",
    str(document_path),
    "--json",
  ], env=command_env)
