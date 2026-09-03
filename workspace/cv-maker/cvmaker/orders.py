"""The CV maker order store (CONTRACT.md sections 2 and 3).

One JSON file, `~/.openclaw/runtime/cv-maker/orders.json`, holding every order
keyed by its order id, plus a per-order artifact directory beside it. An order
carries the applicant's name, phone, email and address, so the store is written
the same way Job Scout writes its agent work packets: an flock around the
read-modify-write, a private temp file, fsync, then os.replace, with 0600 files
inside a 0700 directory.

Retention is part of the store, not an afterthought: `data`, `transcript`,
`sourcePdf` and the rendered PDF go the moment an order settles, and an order
older than seven days disappears from every read and is deleted by `sweep()`.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil

from jobscout.util import normalize_phone, utc_now

from . import CvMakerError


STORE_VERSION = 1
DEFAULT_RUNTIME_ROOT = Path.home() / ".openclaw" / "runtime" / "cv-maker"
RUNTIME_ROOT_ENV = "CV_MAKER_RUNTIME_ROOT"

STATUS_INTERVIEWING = "interviewing"
STATUS_AWAITING_PAYMENT = "awaiting_payment"
STATUS_AWAITING_APPROVAL = "awaiting_approval"
STATUS_SENT = "sent"
STATUS_REJECTED = "rejected"

STATUSES = (
  STATUS_INTERVIEWING,
  STATUS_AWAITING_PAYMENT,
  STATUS_AWAITING_APPROVAL,
  STATUS_SENT,
  STATUS_REJECTED,
)
TERMINAL_STATUSES = frozenset({STATUS_SENT, STATUS_REJECTED})

MODES = ("interview", "revamp")

RETENTION_DAYS = 7
MAX_TRANSCRIPT_TURNS = 40
MAX_TURN_CHARS = 4000
TURN_ROLES = ("assistant", "user")

# Everything a caller may hand to update_order. A status change has side effects
# (timestamps, artifact purge), so it only ever goes through set_status.
MUTABLE_FIELDS = frozenset({
  "data",
  "transcript",
  "sourcePdf",
  "mode",
  "paidClaimedAt",
  "decidedAt",
  "delivered",
  "filename",
  "messageId",
})

# An order id is typed back by an admin as "cv approve <id>" and reaches the
# CLI as an argv value, so it must never begin with "-": argparse would read it
# as a flag and the order could never be settled. Ids therefore start with an
# alphanumeric. token_urlsafe draws from [A-Za-z0-9_-], so candidates that lead
# with a dash or underscore are simply redrawn.
ORDER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def empty_interview_data() -> dict:
  """The InterviewData shape from CONTRACT.md section 4, all fields blank."""
  return {
    "fullName": "",
    "email": "",
    "phone": "",
    "location": "",
    "summary": "",
    "skills": "",
    "experience": [],
    "education": [],
    "referees": [],
  }


def runtime_root() -> Path:
  configured = os.environ.get(RUNTIME_ROOT_ENV, "").strip()
  return Path(configured).expanduser() if configured else DEFAULT_RUNTIME_ROOT


def orders_path() -> Path:
  return runtime_root() / "orders.json"


def safe_order_id(value: object) -> str:
  text = str(value or "").strip()
  if not ORDER_ID_RE.fullmatch(text):
    raise CvMakerError("that CV order reference is not valid")
  return text


def order_dir(order_id: str) -> Path:
  return runtime_root() / safe_order_id(order_id)


def ensure_order_dir(order_id: str) -> Path:
  path = order_dir(order_id)
  path.mkdir(parents=True, exist_ok=True)
  _harden(path, 0o700)
  return path


def _harden(path: Path, mode: int) -> None:
  try:
    os.chmod(path, mode)
  except OSError:
    pass


def _ensure_root() -> Path:
  root = runtime_root()
  root.mkdir(parents=True, exist_ok=True)
  _harden(root, 0o700)
  return root


@contextmanager
def _store_lock():
  """Serialize every read-modify-write, so concurrent turns cannot clobber."""
  lock_path = _ensure_root() / "orders.lock"
  with lock_path.open("a+", encoding="utf-8") as stream:
    _harden(lock_path, 0o600)
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
    try:
      yield
    finally:
      fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _read_store() -> dict:
  path = orders_path()
  try:
    value = json.loads(path.read_text(encoding="utf-8"))
  except FileNotFoundError:
    return {"version": STORE_VERSION, "orders": {}}
  except (OSError, json.JSONDecodeError) as err:
    raise CvMakerError("the CV order store could not be read") from err
  if not isinstance(value, dict) or not isinstance(value.get("orders"), dict):
    raise CvMakerError("the CV order store is not in the expected shape")
  value.setdefault("version", STORE_VERSION)
  return value


def _write_store(store: dict) -> None:
  """Atomic, private write: tmp at 0600, fsync, then os.replace."""
  _ensure_root()
  path = orders_path()
  tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
  try:
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
      json.dump(store, stream, indent=2, sort_keys=True)
      stream.write("\n")
      stream.flush()
      os.fsync(stream.fileno())
    os.replace(tmp, path)
    _harden(path, 0o600)
  finally:
    try:
      tmp.unlink()
    except FileNotFoundError:
      pass


def _parse_time(value: object) -> datetime | None:
  text = str(value or "").strip()
  if not text:
    return None
  try:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
  except ValueError:
    return None
  return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# Retention applies to orders that are finished or abandoned. An order in
# awaiting_payment or awaiting_approval is one the business still owes something
# on: the buyer has completed an interview, and in the approval case has told us
# they paid and is waiting on a human. Ageing those out would delete a paid order
# and leave the admin's "cv approve" answering "not found", with the money taken
# and no record to reconcile against. They are kept until they are settled.
RETAINED_STATUSES = frozenset({STATUS_AWAITING_PAYMENT, STATUS_AWAITING_APPROVAL})


def _is_expired(order: dict, max_age_days: int = RETENTION_DAYS) -> bool:
  if str(order.get("status") or "") in RETAINED_STATUSES:
    return False
  stamp = _parse_time(order.get("createdAt")) or _parse_time(order.get("updatedAt"))
  if stamp is None:
    return False
  return stamp < datetime.now(timezone.utc) - timedelta(days=max(0, int(max_age_days)))


def _live_orders(store: dict) -> list[dict]:
  """Every order a caller may still see, newest first.

  Expired orders are invisible to reads even before `sweep()` deletes them, so a
  stale record can never be resurrected by a late message.
  """
  rows = [row for row in store["orders"].values() if isinstance(row, dict) and not _is_expired(row)]
  rows.sort(key=lambda row: str(row.get("createdAt") or ""), reverse=True)
  return rows


def _new_order_id(existing: dict) -> str:
  for _ in range(64):
    candidate = secrets.token_urlsafe(6)
    if ORDER_ID_RE.fullmatch(candidate) and candidate not in existing:
      return candidate
  raise CvMakerError("could not allocate a CV order reference")


def _cap_transcript(value: object) -> list[dict]:
  rows = value if isinstance(value, list) else []
  turns = [
    {"role": str(row.get("role") or ""), "text": str(row.get("text") or "")[:MAX_TURN_CHARS]}
    for row in rows
    if isinstance(row, dict) and str(row.get("role") or "") in TURN_ROLES
  ]
  return turns[-MAX_TRANSCRIPT_TURNS:]


def _clear_artifact_fields(order: dict) -> None:
  order["data"] = {}
  order["transcript"] = []
  order["sourcePdf"] = None


def _remove_artifact_files(order_id: str, source_pdf: str = "") -> None:
  """Delete the order directory (rendered PDF, copied source) off the lock."""
  shutil.rmtree(order_dir(order_id), ignore_errors=True)
  candidate = str(source_pdf or "").strip()
  if not candidate:
    return
  path = Path(candidate).expanduser()
  try:
    resolved = path.resolve()
    root = runtime_root().resolve()
  except OSError:
    return
  # Only ever unlink inside our own runtime root: a stored path pointing
  # anywhere else is not this service's file to delete.
  if resolved == root or root not in resolved.parents:
    return
  try:
    resolved.unlink()
  except (OSError, FileNotFoundError):
    pass


def create_order(phone: str, mode: str = "interview") -> dict:
  """Open a fresh order in the `interviewing` state for a normalized number."""
  target = normalize_phone(phone)
  chosen = str(mode or "").strip() or "interview"
  if chosen not in MODES:
    raise CvMakerError("that is not a CV order type this service offers")
  now = utc_now()
  with _store_lock():
    store = _read_store()
    order_id = _new_order_id(store["orders"])
    order = {
      "orderId": order_id,
      "phone": target,
      "status": STATUS_INTERVIEWING,
      "mode": chosen,
      "data": empty_interview_data(),
      "transcript": [],
      "sourcePdf": None,
      "createdAt": now,
      "updatedAt": now,
      "paidClaimedAt": None,
      "decidedAt": None,
    }
    store["orders"][order_id] = order
    _write_store(store)
  return copy.deepcopy(order)


def load_order(order_id: str) -> dict | None:
  with _store_lock():
    store = _read_store()
    order = store["orders"].get(safe_order_id(order_id))
  if not isinstance(order, dict) or _is_expired(order):
    return None
  return copy.deepcopy(order)


def active_order_for(phone: str) -> dict | None:
  """The newest order for this number that has not settled yet."""
  target = normalize_phone(phone)
  with _store_lock():
    store = _read_store()
    for order in _live_orders(store):
      if order.get("phone") == target and str(order.get("status") or "") not in TERMINAL_STATUSES:
        return copy.deepcopy(order)
  return None


def list_orders(status: str | None = None) -> list[dict]:
  """Every live order, newest first, optionally narrowed to one status."""
  wanted = str(status or "").strip()
  if wanted and wanted not in STATUSES:
    raise CvMakerError("that is not a CV order status")
  with _store_lock():
    store = _read_store()
    rows = _live_orders(store)
  return [
    copy.deepcopy(row) for row in rows
    if not wanted or str(row.get("status") or "") == wanted
  ]


def update_order(order_id: str, **fields: object) -> dict:
  """Re-read under the lock, then merge `fields` into the stored order."""
  key = safe_order_id(order_id)
  unknown = sorted(set(fields) - MUTABLE_FIELDS)
  if unknown:
    raise CvMakerError(f"cannot set CV order field: {unknown[0]}")
  with _store_lock():
    store = _read_store()
    order = store["orders"].get(key)
    if not isinstance(order, dict):
      raise CvMakerError("that CV order was not found")
    for name, value in fields.items():
      if name == "transcript":
        order[name] = _cap_transcript(value)
      elif name == "data":
        order[name] = copy.deepcopy(value) if isinstance(value, dict) else {}
      elif name == "mode" and str(value or "") not in MODES:
        raise CvMakerError("that is not a CV order type this service offers")
      elif name == "sourcePdf":
        order[name] = str(value) if value else None
      else:
        order[name] = value
    order["updatedAt"] = utc_now()
    _write_store(store)
    return copy.deepcopy(order)


def append_turn(order_id: str, role: str, text: str) -> dict:
  """Append one transcript turn, keeping only the most recent 40."""
  key = safe_order_id(order_id)
  speaker = str(role or "").strip()
  if speaker not in TURN_ROLES:
    raise CvMakerError("that is not a valid conversation role")
  line = str(text or "").strip()[:MAX_TURN_CHARS]
  with _store_lock():
    store = _read_store()
    order = store["orders"].get(key)
    if not isinstance(order, dict):
      raise CvMakerError("that CV order was not found")
    transcript = _cap_transcript(order.get("transcript"))
    transcript.append({"role": speaker, "text": line})
    order["transcript"] = transcript[-MAX_TRANSCRIPT_TURNS:]
    order["updatedAt"] = utc_now()
    _write_store(store)
    return copy.deepcopy(order)


def set_status(order_id: str, status: str) -> str:
  """Move an order along, idempotently, and return the settled status.

  A settled order never moves again and never repeats a side effect: approving
  an order that is already `sent` returns `sent` and delivers nothing.
  """
  key = safe_order_id(order_id)
  wanted = str(status or "").strip()
  if wanted not in STATUSES:
    raise CvMakerError("that is not a CV order status")
  purge_source = ""
  purge = False
  with _store_lock():
    store = _read_store()
    order = store["orders"].get(key)
    if not isinstance(order, dict):
      raise CvMakerError("that CV order was not found")
    current = str(order.get("status") or "")
    if current in TERMINAL_STATUSES or current == wanted:
      return current
    now = utc_now()
    order["status"] = wanted
    order["updatedAt"] = now
    if wanted == STATUS_AWAITING_APPROVAL and not order.get("paidClaimedAt"):
      order["paidClaimedAt"] = now
    if wanted in TERMINAL_STATUSES:
      order["decidedAt"] = now
      purge_source = str(order.get("sourcePdf") or "")
      purge = True
      _clear_artifact_fields(order)
    _write_store(store)
  if purge:
    _remove_artifact_files(key, purge_source)
  return wanted


def purge_artifacts(order_id: str) -> None:
  """Drop the rendered PDF, the source PDF, `data` and `transcript`."""
  key = safe_order_id(order_id)
  source = ""
  with _store_lock():
    store = _read_store()
    order = store["orders"].get(key)
    if isinstance(order, dict):
      source = str(order.get("sourcePdf") or "")
      _clear_artifact_fields(order)
      order["updatedAt"] = utc_now()
      _write_store(store)
  _remove_artifact_files(key, source)


def sweep(max_age_days: int = RETENTION_DAYS) -> int:
  """Delete orders past their retention window. Returns how many went."""
  expired: list[tuple[str, str]] = []
  with _store_lock():
    store = _read_store()
    for key, order in list(store["orders"].items()):
      if not isinstance(order, dict) or _is_expired(order, max_age_days):
        expired.append((key, str((order or {}).get("sourcePdf") or "") if isinstance(order, dict) else ""))
        store["orders"].pop(key, None)
    if expired:
      _write_store(store)
  for key, source in expired:
    try:
      _remove_artifact_files(key, source)
    except CvMakerError:
      continue
  return len(expired)
