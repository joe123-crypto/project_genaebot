#!/usr/bin/env python3
"""Apply stable, sanitized OpenClaw config settings without clobbering state."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile


def deep_merge(target: dict, patch: dict) -> dict:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            deep_merge(target[key], value)
        else:
            target[key] = value
    return target


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: patch-openclaw-config.py TEMPLATE LIVE_CONFIG", file=sys.stderr)
        return 2

    template_path = Path(sys.argv[1])
    live_path = Path(sys.argv[2])

    patch = json.loads(template_path.read_text(encoding="utf-8"))
    live = json.loads(live_path.read_text(encoding="utf-8")) if live_path.exists() else {}
    if not isinstance(patch, dict) or not isinstance(live, dict):
        raise SystemExit("template and live config must be JSON objects")

    merged = deep_merge(live, patch)

    with NamedTemporaryFile("w", encoding="utf-8", dir=str(live_path.parent), delete=False) as fh:
        json.dump(merged, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
        tmp_name = fh.name

    Path(tmp_name).replace(live_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
