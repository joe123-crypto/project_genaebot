"""Shared environment / endpoint resolution for Agent Genaie client tools.

Single source of truth for where the openclaw tools talk to the Agent Genaie
server. The target URL is never hardcoded in the individual tools; it is resolved
here from environment variables (process env first, then the openclaw .env file),
so changing the server or a client is a one-line edit in:

    /home/joseph/.openclaw/.env        (interactive runs)
    /home/joseph/.openclaw/gateway.systemd.env  (gateway-managed runs)

Resolution order for the internal API base URL:
    AGENT_GENAI_BASE_URL  (explicit override, kept for backwards compat)
      -> APP_BASE_URL
      -> derived from AGENT_GENAI_MAIL_URL (strip the /internal/gmail/* suffix)
      -> DEFAULT_BASE_URL (the one and only fallback constant)

Resolution order for the public base URL (user-facing links):
    PUBLIC_BASE_URL
      -> APP_BASE_URL
      -> DEFAULT_BASE_URL
"""

from __future__ import annotations

import os
from pathlib import Path


DEFAULT_ENV_PATH = Path(os.environ.get("OPENCLAW_ENV_PATH", "/home/joseph/.openclaw/.env"))

# The single hardcoded fallback. Overridden by APP_BASE_URL (or AGENT_GENAI_BASE_URL) in .env.
DEFAULT_BASE_URL = "http://localhost:3000"

_MAIL_SUFFIXES = ("/internal/gmail/send", "/internal/gmail/senders")


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
    """Process environment first (how the gateway injects managed keys), then .env."""
    if os.environ.get(name):
        return os.environ[name]
    return parse_env_file(DEFAULT_ENV_PATH).get(name, fallback)


def agent_base_url() -> str:
    if env_default("AGENT_GENAI_BASE_URL"):
        return env_default("AGENT_GENAI_BASE_URL").rstrip("/")
    if env_default("APP_BASE_URL"):
        return env_default("APP_BASE_URL").rstrip("/")
    mail_url = env_default("AGENT_GENAI_MAIL_URL", "")
    for suffix in _MAIL_SUFFIXES:
        if mail_url.endswith(suffix):
            return mail_url[: -len(suffix)].rstrip("/")
    return DEFAULT_BASE_URL


def agent_base_url_source() -> str:
    if env_default("AGENT_GENAI_BASE_URL"):
        return "AGENT_GENAI_BASE_URL"
    if env_default("APP_BASE_URL"):
        return "APP_BASE_URL"
    mail_url = env_default("AGENT_GENAI_MAIL_URL", "")
    for suffix in _MAIL_SUFFIXES:
        if mail_url.endswith(suffix):
            return "AGENT_GENAI_MAIL_URL"
    return "default"


def public_base_url() -> str:
    """Public-facing base URL for user-visible links (setup pages, OAuth callbacks)."""
    if env_default("PUBLIC_BASE_URL"):
        return env_default("PUBLIC_BASE_URL").rstrip("/")
    if env_default("APP_BASE_URL"):
        return env_default("APP_BASE_URL").rstrip("/")
    return DEFAULT_BASE_URL


def public_base_url_source() -> str:
    if env_default("PUBLIC_BASE_URL"):
        return "PUBLIC_BASE_URL"
    if env_default("APP_BASE_URL"):
        return "APP_BASE_URL"
    return "default"


def endpoint(path: str) -> str:
    return f"{agent_base_url()}{path}"


def mail_url() -> str:
    """Gmail-send endpoint. Explicit AGENT_GENAI_MAIL_URL wins; else derived from base."""
    explicit = env_default("AGENT_GENAI_MAIL_URL", "")
    if explicit:
        return explicit
    return endpoint("/internal/gmail/send")


def api_key() -> str:
    return env_default("AGENT_GENAI_INTERNAL_API_KEY", "")
