#!/usr/bin/env python3
"""Sync only the optional runtime plugin enablement leaves from the template.

The full template is not applied on Railway. Never replace plugins, entries or
plugin configs: live credentials, account routing and required plugin options
must survive. A malformed policy leaves the live file untouched.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

# Deliberately excludes model access, memory, file transfer, local execution and
# all WhatsApp plugins. Turning an integration back on in the template is enough
# to roll its policy back on the next boot.
GOVERNED_PLUGINS = (
    "anthropic", "browser", "canvas", "cua-computer", "device-pair",
    "geolocation", "github", "ollama", "talk-voice", "voice-call", "xai",
)


def object_child(parent: dict, key: str) -> dict:
    value = parent.setdefault(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def sync(template_path: Path, config_path: Path, dry_run: bool = False) -> list[str]:
    template = json.loads(template_path.read_text(encoding="utf-8"))
    live = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(template, dict) or not isinstance(live, dict):
        raise ValueError("template and live config must be objects")
    policy = object_child(object_child(template, "plugins"), "entries")
    entries = object_child(object_child(live, "plugins"), "entries")
    changes = []
    for plugin in GOVERNED_PLUGINS:
        wanted = policy.get(plugin, {}).get("enabled")
        if not isinstance(wanted, bool):
            raise ValueError(f"template plugins.entries.{plugin}.enabled must be boolean")
        entry = object_child(entries, plugin)
        if type(entry.get("enabled")) is bool and entry["enabled"] == wanted:
            continue
        entry["enabled"] = wanted
        changes.append(f"{plugin}={str(wanted).lower()}")
    if not changes:
        return ["runtime plugins unchanged"]
    if dry_run:
        return ["runtime plugins DRY RUN: " + ", ".join(changes)]
    stat = config_path.stat()
    fd, temporary = tempfile.mkstemp(dir=config_path.parent, prefix=".runtime-plugins-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(live, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.chmod(temporary, stat.st_mode & 0o7777)
        if (os.getuid(), os.getgid()) != (stat.st_uid, stat.st_gid):
            os.chown(temporary, stat.st_uid, stat.st_gid)
        os.replace(temporary, config_path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return ["runtime plugins updated: " + ", ".join(changes)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template", type=Path)
    parser.add_argument("config", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        for line in sync(args.template, args.config, args.dry_run):
            print(line)
    except Exception as error:  # never turn an optional optimization into an outage
        print(f"runtime plugins NOT synced: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
