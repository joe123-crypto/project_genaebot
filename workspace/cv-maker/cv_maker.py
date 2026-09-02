#!/usr/bin/env python3
"""Entry point for the standalone CV maker service.

Run through workspace/bin/cv-maker, which sets PYTHONPATH. Invoked directly it
still works: the Job Scout tree is located next to this one and appended to
sys.path, because the CV pipeline (HTML builders, the PDF validators, the phone
and text helpers) is imported from there and never copied.

stdout is always one JSON object, including when the engine cannot start.
"""

from __future__ import annotations

import json
from pathlib import Path
import os
import sys


def _extend_import_path() -> None:
  here = Path(__file__).resolve().parent
  candidates = [here]
  configured = os.environ.get("JOB_SCOUT_SOURCE_ROOT", "").strip()
  if configured:
    candidates.append(Path(configured).expanduser())
  candidates.append(here.parent / "job-scout")
  for candidate in candidates:
    entry = str(candidate)
    if candidate.is_dir() and entry not in sys.path:
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
