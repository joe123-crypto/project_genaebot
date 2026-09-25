"""Tiny shared helpers for reading and atomically writing JSON state files.

Several deterministic cron jobs keep a small JSON state file next to a
``runtime/cron-state/<name>/state.json`` path and update it with the same
"write a temp file, then ``os.replace`` it into place" pattern so a crash mid-write
never leaves a half-written state file. That primitive was copy-pasted into each
job; it now lives here.

Std-lib only, so it is importable from the OpenClaw ``--command`` cron scripts
under ``workspace/cron/`` (which are std-lib only by contract).

This is deliberately minimal: it owns the read + atomic-write primitive, not any
particular state *shape*.
"""

import json
import os


def load_json(path, default):
    """Return the parsed JSON at ``path``, or ``default`` if it is missing or
    unreadable/unparseable. Callers pass a fresh mutable default (e.g. ``{}``)
    and apply their own ``setdefault`` for expected keys, matching the previous
    per-job ``load_state`` behavior."""
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def write_json_atomic(path, payload, indent=2):
    """Write ``payload`` as JSON to ``path`` atomically: create the parent
    directory, write a sibling ``*.tmp`` file, then ``os.replace`` it over the
    target so readers never observe a partial write. Mirrors the previous
    per-job ``save_state``."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=indent)
    os.replace(tmp, path)
