"""Dispatch-run marker persistence and the exclusive dispatch lock.

Reads/writes the per-run JSON markers and candidate artifacts under the dispatch
root, and guards a run with an flock so two dispatches can't overlap.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import secrets

from .constants import DEFAULT_DISPATCH_ROOT
from .profile import user_key
from .util import safe_run_id, utc_now


AGENT_RUN_STALE_SECONDS = 7 * 60 * 60


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


def load_dispatch_optional(run_id: str, root: Path = DEFAULT_DISPATCH_ROOT) -> dict | None:
  """load_dispatch, but None for a missing, unreadable, or invalid marker."""
  try:
    value = json.loads(dispatch_path(run_id, root).read_text(encoding="utf-8"))
  except (OSError, ValueError, SystemExit):
    return None
  return value if isinstance(value, dict) else None


def save_dispatch(data: dict, root: Path = DEFAULT_DISPATCH_ROOT) -> dict:
  run_id = safe_run_id(data.get("runId", ""))
  root.mkdir(parents=True, exist_ok=True)
  path = dispatch_path(run_id, root)
  tmp = path.with_suffix(".json.tmp")
  tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  tmp.replace(path)
  return {"ok": True, "runId": run_id, "path": str(path)}


def _write_json(path: Path, data: dict) -> None:
  """Atomically write private dispatch state.

  Agent work packets contain one subscriber's CV/profile data, so unlike the
  older aggregate marker they must never inherit a permissive umask.
  """
  path.parent.mkdir(parents=True, exist_ok=True)
  tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
  try:
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
      json.dump(data, stream, indent=2, sort_keys=True)
      stream.write("\n")
      stream.flush()
      os.fsync(stream.fileno())
    tmp.replace(path)
    os.chmod(path, 0o600)
  finally:
    try:
      tmp.unlink()
    except FileNotFoundError:
      pass


def _read_json(path: Path, missing_message: str) -> dict:
  if not path.is_file():
    raise SystemExit(missing_message)
  try:
    value = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError) as err:
    raise SystemExit(f"Job Scout agent state is invalid: {path}") from err
  if not isinstance(value, dict):
    raise SystemExit(f"Job Scout agent state is invalid: {path}")
  return value


def agent_run_dir(root: Path, run_id: str) -> Path:
  return Path(root) / safe_run_id(run_id)


def agent_claim_path(root: Path, run_id: str, claim_id: str) -> Path:
  claim = str(claim_id or "").strip()
  if not claim or not all(character.isalnum() or character in "-_" for character in claim):
    raise SystemExit("invalid Job Scout claim id")
  return agent_run_dir(root, run_id) / "claims" / f"{claim}.json"


def agent_work_path(root: Path, run_id: str, claim_id: str) -> Path:
  return agent_run_dir(root, run_id) / "work" / f"{str(claim_id)}.json"


def agent_claim_output_dir(root: Path, run_id: str, claim_id: str) -> Path:
  return agent_run_dir(root, run_id) / "work" / str(claim_id)


def active_agent_dispatch_path(root: Path) -> Path:
  return Path(root) / "active-agent-dispatch.json"


@contextmanager
def agent_state_lock(root: Path):
  """Serialize short state transitions without serializing delivery work."""
  path = Path(root) / "agent-state.lock"
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open("a+", encoding="utf-8") as stream:
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
    try:
      yield
    finally:
      fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def load_agent_claim(root: Path, run_id: str, claim_id: str) -> dict:
  return _read_json(
    agent_claim_path(root, run_id, claim_id),
    f"Job Scout agent claim not found: {str(claim_id)}",
  )


def load_agent_work(root: Path, run_id: str, claim_id: str) -> dict:
  return _read_json(
    agent_work_path(root, run_id, claim_id),
    f"Job Scout agent work packet not found: {str(claim_id)}",
  )


def save_agent_claim(root: Path, run_id: str, claim_id: str, data: dict) -> None:
  _write_json(agent_claim_path(root, run_id, claim_id), data)


def save_agent_work(root: Path, run_id: str, claim_id: str, data: dict) -> None:
  _write_json(agent_work_path(root, run_id, claim_id), data)


def active_agent_dispatch(root: Path) -> dict | None:
  path = active_agent_dispatch_path(root)
  if not path.is_file():
    return None
  return _read_json(path, "active Job Scout agent dispatch not found")


def set_active_agent_dispatch(root: Path, data: dict) -> None:
  _write_json(active_agent_dispatch_path(root), data)


def clear_active_agent_dispatch(root: Path, run_id: str) -> None:
  path = active_agent_dispatch_path(root)
  if not path.is_file():
    return
  active = _read_json(path, "active Job Scout agent dispatch not found")
  if safe_run_id(active.get("runId", "")) == safe_run_id(run_id):
    path.unlink()


def fail_agent_dispatch(root: Path, run_id: str, error: str) -> bool:
  """Finish an uncompleted run as failed and release its active pointer.

  Caller must hold agent_state_lock.
  """
  marker = load_dispatch_optional(run_id, root)
  if marker is None or marker.get("finishedAt"):
    clear_active_agent_dispatch(root, run_id)
    return False
  finished_at = utc_now()
  marker.update({
    "phase": "failed",
    "preparationError": str(error or "Job Scout preparation failed")[:240],
    "finishedAt": finished_at,
    "updatedAt": finished_at,
  })
  save_dispatch(marker, root)
  clear_active_agent_dispatch(root, run_id)
  return True


def iso_age_seconds(value: object) -> float | None:
  try:
    text = str(value or "").replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
      parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
  except (TypeError, ValueError):
    return None


def resolve_active_agent_run(root: Path, stale_after_seconds: int = AGENT_RUN_STALE_SECONDS) -> str | None:
  """Return the run id of a live agent dispatch, clearing markers that cannot
  block anymore. Caller must hold agent_state_lock.

  A finished, stale, or orphaned (no readable run marker) active pointer is
  cleared instead of returned: none of those runs can ever finish on their
  own, and leaving the pointer behind would block every future dispatch.
  """
  active = active_agent_dispatch(root)
  if not active:
    return None
  run_id = str(active.get("runId") or "").strip()
  marker = load_dispatch_optional(run_id, root) if run_id else None
  if marker is None:
    active_agent_dispatch_path(root).unlink(missing_ok=True)
    return None
  if marker.get("finishedAt"):
    clear_active_agent_dispatch(root, run_id)
    return None
  age = iso_age_seconds(active.get("startedAt"))
  if age is not None and age > stale_after_seconds:
    fail_agent_dispatch(
      root,
      run_id,
      "Job Scout preparation did not finish before the active-run timeout",
    )
    return None
  return safe_run_id(run_id)


def assert_no_active_agent_dispatch(root: Path, stale_after_seconds: int = AGENT_RUN_STALE_SECONDS) -> None:
  """Keep legacy/manual dispatch from overlapping an agent-orchestrated run."""
  with agent_state_lock(root):
    run_id = resolve_active_agent_run(root, stale_after_seconds)
    if run_id:
      raise SystemExit(f"Job Scout agent dispatch {run_id} is still active")


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
