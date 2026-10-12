#!/usr/bin/env python3
"""Copy the tracked public WhatsApp system prompt into the live openclaw.json.

The public agent's instructions live in the volume's openclaw.json at
channels.whatsapp.direct["*"].systemPrompt. Nothing else on the Railway boot
path applies config/openclaw.template.json (see README), so without this step a
change to workspace/docs/public-whatsapp-system-prompt.txt never reaches the
bot. Only that one key is written; the rest of the live config is left alone.

Usage: sync-public-prompt.py <prompt.txt> <openclaw.json> [<patch-openclaw-config.py>]

Never fatal: any problem is reported on stderr and the config is left as it
was, so a bad prompt file cannot stop the gateway from starting.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

DEFAULT_APPLIER = Path(__file__).resolve().parent.parent / "patch-openclaw-config.py"


def load_setter(applier: Path):
    spec = importlib.util.spec_from_file_location("patch_openclaw_config", applier)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.set_public_whatsapp_prompt


def current_prompt(config: dict):
    node = config
    for key in ("channels", "whatsapp", "direct", "*"):
        node = node.get(key) if isinstance(node, dict) else None
    return node.get("systemPrompt") if isinstance(node, dict) else None


def sync(prompt_path: Path, config_path: Path, applier: Path = DEFAULT_APPLIER) -> str:
    """Returns "updated" or "unchanged". Raises on any problem."""
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError(f"{prompt_path} is empty")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if current_prompt(config) == prompt:
        return "unchanged"
    load_setter(applier)(config, prompt)

    stat = config_path.stat()
    fd, tmp = tempfile.mkstemp(dir=config_path.parent, prefix=f".{config_path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.chmod(tmp, stat.st_mode & 0o7777)
        try:
            os.chown(tmp, stat.st_uid, stat.st_gid)
        except PermissionError:
            pass
        os.replace(tmp, config_path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return "updated"


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    applier = Path(argv[3]) if len(argv) == 4 else DEFAULT_APPLIER
    try:
        result = sync(Path(argv[1]), Path(argv[2]), applier)
    except Exception as err:  # noqa: BLE001 - never block the boot
        print(f"public WhatsApp prompt NOT synced: {err}", file=sys.stderr)
        return 0
    print(f"public WhatsApp prompt {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
