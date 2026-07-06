"""Shared allowlist resolution from the invite store + openclaw config.

Extracted from the public-events / public-news registry scripts. The public-news
script supported a legacy invites path; that behavior is preserved here via the
optional ``default_store`` / ``legacy_store`` arguments (when both are provided
and the requested store is the default one but is missing, the legacy store is
used instead). Callers that pass neither get the plain behavior.
"""

from __future__ import annotations

import json
import pathlib

from phone_utils import normalize_phone


def load_unlocked_invite_members(
    invites_store: pathlib.Path,
    *,
    default_store: pathlib.Path | None = None,
    legacy_store: pathlib.Path | None = None,
) -> set[str]:
    if (
        legacy_store is not None
        and default_store is not None
        and not invites_store.exists()
        and invites_store == default_store
        and legacy_store.exists()
    ):
        invites_store = legacy_store
    if not invites_store.exists():
        return set()
    payload = json.loads(invites_store.read_text(encoding="utf-8"))
    members = payload.get("members", {})
    if not isinstance(members, dict):
        return set()
    allowlisted = set()
    for phone, record in members.items():
        if not isinstance(record, dict) or record.get("status") != "unlocked":
            continue
        try:
            allowlisted.add(normalize_phone(record.get("phone") or phone))
        except ValueError:
            continue
    return allowlisted


def load_allowlisted(
    config_path: pathlib.Path,
    invites_store: pathlib.Path,
    *,
    default_store: pathlib.Path | None = None,
    legacy_store: pathlib.Path | None = None,
) -> set[str]:
    normalized = load_unlocked_invite_members(
        invites_store, default_store=default_store, legacy_store=legacy_store
    )
    if not config_path.exists():
        return normalized
    config = json.loads(config_path.read_text(encoding="utf-8"))
    whatsapp = config.get("channels", {}).get("whatsapp", {})
    values = list(whatsapp.get("allowFrom", []) or [])
    direct = whatsapp.get("direct", {})
    if isinstance(direct, dict):
        values.extend(direct.keys())
    public_gate = config.get("plugins", {}).get("entries", {}).get("public-service-gate", {}).get("config", {})
    values.extend(public_gate.get("publicUsers", []) or [])
    values.extend(public_gate.get("whatsappAdminUsers", []) or [])
    for value in values:
        try:
            normalized.add(normalize_phone(value))
        except ValueError:
            continue
    return normalized
