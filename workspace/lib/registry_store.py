"""Shared JSON subscription-registry helpers: atomic writes + fcntl locking.

Extracted verbatim from the public-events / public-news registry scripts, which
used byte-identical copies. The registry shape is ``{"version": 1,
"subscribers": {...}}``.
"""

from __future__ import annotations

import fcntl
import json
import os
import pathlib
import tempfile
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: pathlib.Path) -> dict:
    if not path.exists():
        return {"version": 1, "subscribers": {}}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {"version": 1, "subscribers": {}}
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("registry root must be a JSON object")
    payload.setdefault("version", 1)
    payload.setdefault("subscribers", {})
    if not isinstance(payload["subscribers"], dict):
        raise ValueError("registry subscribers must be a JSON object")
    return payload


def write_json(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def with_locked_registry(path: pathlib.Path, mutate):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        payload = load_json(path)
        result = mutate(payload)
        write_json(path, payload)
        return result
