"""Configuration adapter for the external Agent Genaie internal API."""

from __future__ import annotations

from ..config import DEFAULT_AGENT_BASE_URL, env_default


_MAIL_SUFFIXES = ("/internal/gmail/send", "/internal/gmail/senders")


def agent_base_url() -> str:
  if env_default("AGENT_GENAI_BASE_URL"):
    return env_default("AGENT_GENAI_BASE_URL").rstrip("/")
  if env_default("APP_BASE_URL"):
    return env_default("APP_BASE_URL").rstrip("/")
  mail_url = env_default("AGENT_GENAI_MAIL_URL", "")
  for suffix in _MAIL_SUFFIXES:
    if mail_url.endswith(suffix):
      return mail_url[: -len(suffix)].rstrip("/")
  return DEFAULT_AGENT_BASE_URL


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


def endpoint(path: str) -> str:
  return f"{agent_base_url()}{path}"


def mail_url() -> str:
  return env_default("AGENT_GENAI_MAIL_URL", "") or endpoint("/internal/gmail/send")


def api_key() -> str:
  return env_default("AGENT_GENAI_INTERNAL_API_KEY", "")
