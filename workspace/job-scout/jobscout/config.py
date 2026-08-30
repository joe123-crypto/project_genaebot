"""Owned configuration defaults for the standalone Job Scout package.

Path settings intentionally use process-environment overrides only. Agent
Genaie credentials and URLs additionally fall back to OpenClaw's ``.env`` via
``env_default`` so existing gateway and interactive execution keep the same
resolution order.
"""

from __future__ import annotations

import os
from pathlib import Path
import urllib.parse


PRODUCTION_OPENCLAW_ROOT = Path("/home/joseph/.openclaw")
PRODUCTION_WORKSPACE_ROOT = PRODUCTION_OPENCLAW_ROOT / "workspace"
PRODUCTION_RUNTIME_ROOT = PRODUCTION_OPENCLAW_ROOT / "runtime" / "job-scout"
PRODUCTION_PENDING_LINKS_PATH = PRODUCTION_OPENCLAW_ROOT / "runtime" / "account-link" / "pending-links.json"
PRODUCTION_CHANNEL_RUNTIME_ROOT = PRODUCTION_OPENCLAW_ROOT / "runtime" / "job-scout-channel"
PRODUCTION_CHANNEL_DB_PATH = PRODUCTION_CHANNEL_RUNTIME_ROOT / "posts.sqlite3"
PRODUCTION_CHANNEL_HEALTH_PATH = PRODUCTION_CHANNEL_RUNTIME_ROOT / "health.json"
PRODUCTION_GENAIE_MAIL_BIN = PRODUCTION_WORKSPACE_ROOT / "bin" / "genaie-mail"

DEFAULT_AGENT_BASE_URL = "http://localhost:3000"
DEFAULT_WEB_APPLY_URL = "http://job-scout-browser:8787/apply"
DEFAULT_ENV_PATH = Path(os.environ.get("OPENCLAW_ENV_PATH", str(PRODUCTION_OPENCLAW_ROOT / ".env"))).expanduser()


def configured_path(name: str, default: Path) -> Path:
  value = os.environ.get(name, "").strip()
  return Path(value).expanduser() if value else default


WORKSPACE_ROOT = configured_path("JOB_SCOUT_WORKSPACE_ROOT", PRODUCTION_WORKSPACE_ROOT)
RUNTIME_ROOT = configured_path("JOB_SCOUT_RUNTIME_ROOT", PRODUCTION_RUNTIME_ROOT)
PENDING_LINKS_PATH = configured_path("OPENCLAW_PENDING_LINKS_PATH", PRODUCTION_PENDING_LINKS_PATH)
CHANNEL_DB_PATH = configured_path("JOB_SCOUT_CHANNEL_DB", PRODUCTION_CHANNEL_DB_PATH)
CHANNEL_HEALTH_PATH = configured_path("JOB_SCOUT_CHANNEL_HEALTH", PRODUCTION_CHANNEL_HEALTH_PATH)
GENAIE_MAIL_BIN = configured_path("GENAIE_MAIL_BIN", WORKSPACE_ROOT / "bin" / "genaie-mail")
OPENCLAW_BIN_FALLBACKS = (
  Path("/usr/local/bin/openclaw"),
  Path("/home/joseph/.npm-global/bin/openclaw"),
)


def parse_env_file(path: Path) -> dict[str, str]:
  values: dict[str, str] = {}
  if not path.exists():
    return values
  for raw_line in path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
      continue
    if line.startswith("export "):
      line = line[len("export ") :].strip()
    if "=" not in line:
      continue
    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and ((value[0] == value[-1] == '"') or (value[0] == value[-1] == "'")):
      value = value[1:-1]
    if key:
      values[key] = value
  return values


def env_default(name: str, fallback: str = "") -> str:
  """Resolve a setting from the process environment, then OpenClaw's .env."""
  if os.environ.get(name):
    return os.environ[name]
  return parse_env_file(DEFAULT_ENV_PATH).get(name, fallback)


def personalization_enabled(resolver=env_default) -> bool:
  """Whether Job Scout personalizes CVs and submits applications.

  Disabled by default. The group feed, CV builder, and advertising crons keep
  running; only per-user personalization and application submission are gated.
  Set JOB_SCOUT_PERSONALIZATION_ENABLED to a truthy value to re-enable.
  """
  value = str(resolver("JOB_SCOUT_PERSONALIZATION_ENABLED", "")).strip().casefold()
  return value in {"1", "true", "yes", "on"}


def web_apply_url(resolver=env_default) -> str:
  return resolver("JOB_SCOUT_WEB_APPLY_URL", DEFAULT_WEB_APPLY_URL).strip()


def render_pdf_url(resolver=env_default) -> str:
  configured = resolver("JOB_SCOUT_RENDER_PDF_URL", "").strip()
  if configured:
    return configured
  apply_url = web_apply_url(resolver)
  return urllib.parse.urljoin(apply_url, "/render-pdf") if apply_url else ""
