"""Tiny shared helpers for reading and atomically writing JSON state files.

Several deterministic cron jobs keep a small JSON state file next to a
``runtime/cron-state/<name>/state.json`` path and update it with the same
"write a temp file, then ``os.replace`` it into place" pattern so a crash mid-write
never leaves a half-written state file. That primitive was copy-pasted into each
job; it now lives here.

Std-lib only, so it is importable from the OpenClaw ``--command`` cron scripts
under ``workspace/bin/`` (which are std-lib only by contract).

This is deliberately minimal: it owns the read + atomic-write primitive, not any
particular state *shape*.
"""

import contextlib
import json
import os

try:
    import fcntl
except ImportError:  # pragma: no cover - Linux and macOS both have it
    fcntl = None


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


def _lock_path(path):
    """A lock beside the state file, never renamed over.

    It has to be its own file: ``write_json_atomic`` replaces the target by
    rename, so a lock taken on the state file's inode would stop protecting it
    the moment anyone wrote."""
    return path + ".lock"


@contextlib.contextmanager
def file_lock(path):
    """Hold an exclusive advisory lock for ``path`` while the block runs.

    **Fails open.** If the lock cannot be taken -- no ``fcntl``, a read-only
    directory, a filesystem that does not support it -- the block still runs,
    unlocked. A campaign send must never fail because a lock was unavailable;
    the lock is here to make a concurrent edit safe, not to gate posting.
    """
    if fcntl is None:
        yield False
        return
    handle = None
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        handle = open(_lock_path(path), "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError:
        if handle is not None:
            handle.close()
        yield False
        return
    try:
        yield True
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def locked_write_json_atomic(path, payload, indent=2):
    """``write_json_atomic`` under :func:`file_lock`, so a reader doing a
    read-modify-write under the same lock cannot lose this write."""
    with file_lock(path):
        write_json_atomic(path, payload, indent=indent)


def locked_update_json(path, mutate, default=None, indent=2):
    """Read ``path``, apply ``mutate``, and write it back under one lock.

    ``mutate`` receives the freshly-read value and returns True when it changed
    something worth writing. Reading INSIDE the lock is the point: it is what
    stops this from overwriting a write that landed between an earlier read and
    this one.
    """
    with file_lock(path):
        state = load_json(path, {} if default is None else default)
        if not mutate(state):
            return False
        write_json_atomic(path, state, indent=indent)
        return True
