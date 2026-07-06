"""Dispatch-run marker persistence and the exclusive dispatch lock.

Reads/writes the per-run JSON markers and candidate artifacts under the dispatch
root, and guards a run with an flock so two dispatches can't overlap.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path

from .constants import DEFAULT_DISPATCH_ROOT
from .profile import user_key
from .util import safe_run_id, utc_now


def dispatch_path(run_id: str, root: Path = DEFAULT_DISPATCH_ROOT) -> Path:
  return root / f"{safe_run_id(run_id)}.json"


def load_dispatch(run_id: str, root: Path = DEFAULT_DISPATCH_ROOT) -> dict:
  path = dispatch_path(run_id, root)
  if not path.exists():
    raise SystemExit(f"Dispatch run not found: {safe_run_id(run_id)}")
  try:
    return json.loads(path.read_text(encoding="utf-8"))
  except json.JSONDecodeError as err:
    raise SystemExit(f"Dispatch run marker is invalid JSON: {path}") from err


def save_dispatch(data: dict, root: Path = DEFAULT_DISPATCH_ROOT) -> dict:
  run_id = safe_run_id(data.get("runId", ""))
  root.mkdir(parents=True, exist_ok=True)
  path = dispatch_path(run_id, root)
  tmp = path.with_suffix(".json.tmp")
  tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  tmp.replace(path)
  return {"ok": True, "runId": run_id, "path": str(path)}


def write_candidate_artifact(root: Path, run_id: str, user_id: str, data: dict) -> str:
  artifact_dir = root / safe_run_id(run_id)
  artifact_dir.mkdir(parents=True, exist_ok=True)
  path = artifact_dir / f"{user_key(user_id)}.candidates.json"
  tmp = path.with_suffix(".json.tmp")
  tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  tmp.replace(path)
  return str(path)


def dispatch_lock_path(dispatch_root: Path) -> Path:
  return Path(dispatch_root).resolve() / "dispatch.lock"


@contextmanager
def exclusive_dispatch_lock(dispatch_root: Path, run_id: str):
  lock_path = dispatch_lock_path(dispatch_root)
  lock_path.parent.mkdir(parents=True, exist_ok=True)
  with lock_path.open("a+", encoding="utf-8") as lock_file:
    try:
      fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as err:
      raise SystemExit("another Job Scout dispatch is already running") from err
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(json.dumps({"runId": run_id, "pid": os.getpid(), "startedAt": utc_now()}) + "\n")
    lock_file.flush()
    try:
      yield
    finally:
      fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
