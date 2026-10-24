#!/usr/bin/env python3
"""Entry point for the standalone CV maker service.

Run through workspace/bin/cv-maker, which sets PYTHONPATH. Invoked directly it
still works: this directory is put on sys.path first.

stdout is always one JSON object, including when the engine cannot start.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys


def _extend_import_path() -> None:
  here = Path(__file__).resolve().parent
  entry = str(here)
  if entry not in sys.path:
    sys.path.insert(0, entry)


def _fail(message: str) -> int:
  print(json.dumps({"ok": False, "error": message}))
  return 0


def main() -> int:
  _extend_import_path()
  try:
    from cvmaker.cli import main as run_cli
  except Exception:
    return _fail("the CV service is not installed correctly")
  return run_cli()


if __name__ == "__main__":
  raise SystemExit(main())
