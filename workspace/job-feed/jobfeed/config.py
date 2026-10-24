"""Runtime locations for the job feed.

Path settings use process-environment overrides only.
"""

from __future__ import annotations

import os
from pathlib import Path


PRODUCTION_OPENCLAW_ROOT = Path("/home/joseph/.openclaw")
# The runtime directory keeps its historical "job-scout" name so the existing
# cache and the per-group delivered state (which stops re-posting an advert)
# carry over unchanged.
PRODUCTION_RUNTIME_ROOT = PRODUCTION_OPENCLAW_ROOT / "runtime" / "job-scout"
OPENCLAW_BIN_FALLBACKS = (
  Path("/usr/local/bin/openclaw"),
  Path("/home/joseph/.npm-global/bin/openclaw"),
)


def configured_path(names: tuple[str, ...], default: Path) -> Path:
  for name in names:
    value = os.environ.get(name, "").strip()
    if value:
      return Path(value).expanduser()
  return default


RUNTIME_ROOT = configured_path(("JOB_FEED_RUNTIME_ROOT", "JOB_SCOUT_RUNTIME_ROOT"), PRODUCTION_RUNTIME_ROOT)
