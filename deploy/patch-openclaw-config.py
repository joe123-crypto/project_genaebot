#!/usr/bin/env python3
"""Apply the version-controlled OpenClaw config template to the live config.

Model: the template (``config/openclaw.template.json``) is the authoritative,
version-controlled source of truth for *static* configuration. Applying it is
authoritative — the live config is rebuilt from the template, so a key removed
from the template (e.g. a retired plugin option) also disappears from live.

Exception: a small set of runtime-managed / personal-data paths are NOT kept in
the template (so PII never enters git and runtime state is never clobbered).
These ``PRESERVE_PATHS`` are carried over from the existing live config on each
apply. This list must mirror exactly the paths omitted when the template is
generated from the live config.

The public WhatsApp system prompt is tracked outside the JSON template to avoid
duplicating a large string. Each deploy injects that prompt after preserving
runtime-managed configuration.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile

# Runtime-managed / personal-data paths preserved from the live config.
# Kept out of the version-controlled template; re-injected here from live.
PRESERVE_PATHS: list[list[str]] = [
    ["meta"],                                                                  # runtime: lastTouchedAt timestamps
    ["wizard"],                                                                # runtime: setup-wizard timestamps
    # NOTE: public-service-gate.config.publicUsers is intentionally NOT preserved.
    # It is stale: the invite member store (runtime/public-service-gate/invites.json)
    # is the source of truth, and with invitesEnabled=true the plugin never reads
    # publicUsers. Omitting it lets the authoritative apply prune it from live.
    ["plugins", "entries", "public-service-gate", "config", "whatsappAdminUsers"],  # PII: admin phones
    ["plugins", "entries", "public-service-gate", "config", "botWhatsappNumber"],   # operator: bot phone number
    ["plugins", "entries", "whatsapp-relevance-gate", "config", "bypassUsers"],     # PII: admin phones
    ["channels", "whatsapp", "allowFrom"],                                     # runtime: derived (admin + public)
    ["channels", "whatsapp", "groupAllowFrom"],                               # PII: allowlisted group members
    ["channels", "whatsapp", "direct"],                                        # runtime: system prompt synced from a workspace doc
    ["tools", "elevated", "allowFrom"],                                        # PII: elevated-access phones
]


def get_path(d: dict, path: list[str]):
    """Return (found, value) for a nested path."""
    cur = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return False, None
        cur = cur[key]
    return True, cur


def set_path(d: dict, path: list[str], value) -> None:
    """Set a nested path, creating intermediate dicts as needed."""
    cur = d
    for key in path[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[path[-1]] = value


def set_public_whatsapp_prompt(config: dict, prompt: str) -> None:
    """Install the tracked wildcard prompt without changing peer overrides."""
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("public WhatsApp prompt is empty")

    channels = config.setdefault("channels", {})
    if not isinstance(channels, dict):
        raise ValueError("channels must be an object")
    whatsapp = channels.setdefault("whatsapp", {})
    if not isinstance(whatsapp, dict):
        raise ValueError("channels.whatsapp must be an object")
    direct = whatsapp.setdefault("direct", {})
    if not isinstance(direct, dict):
        raise ValueError("channels.whatsapp.direct must be an object")
    wildcard = direct.setdefault("*", {})
    if not isinstance(wildcard, dict):
        raise ValueError('channels.whatsapp.direct["*"] must be an object')
    wildcard["systemPrompt"] = prompt


def apply_template(live: dict, template: dict, public_whatsapp_prompt: str) -> dict:
    """Authoritative apply of the template, preserving runtime/PII paths from live."""
    merged = json.loads(json.dumps(template))  # deep copy
    for path in PRESERVE_PATHS:
        found, value = get_path(live, path)
        if found:
            set_path(merged, path, json.loads(json.dumps(value)))
    set_public_whatsapp_prompt(merged, public_whatsapp_prompt)
    return merged


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: patch-openclaw-config.py TEMPLATE LIVE_CONFIG PUBLIC_WHATSAPP_PROMPT", file=sys.stderr)
        return 2

    template_path = Path(sys.argv[1])
    live_path = Path(sys.argv[2])
    prompt_path = Path(sys.argv[3])

    template = json.loads(template_path.read_text(encoding="utf-8"))
    live = json.loads(live_path.read_text(encoding="utf-8")) if live_path.exists() else {}
    if not isinstance(template, dict) or not isinstance(live, dict):
        raise SystemExit("template and live config must be JSON objects")

    prompt = prompt_path.read_text(encoding="utf-8")
    merged = apply_template(live, template, prompt)

    with NamedTemporaryFile("w", encoding="utf-8", dir=str(live_path.parent), delete=False) as fh:
        json.dump(merged, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
        tmp_name = fh.name

    Path(tmp_name).replace(live_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
