"""The cv-maker command line (CONTRACT.md section 6).

stdout is always exactly one JSON object and the exit code is always 0, so the
Node plugin's runJsonCommand always has something to parse. Every failure comes
back as {"ok": false, "error": "..."} with the text run through
jobscout.textutil.public_text and then scrubbed of anything path-shaped: a
public user is on the other end of this, so no traceback, no file path, and no
CV content ever reaches stdout.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys

from jobscout.textutil import public_text
from jobscout.util import normalize_phone

from . import CvMakerError
from . import build, delivery, orders


MAX_ERROR_CHARS = 240
MAX_MESSAGE_CHARS = 4000
MAX_SOURCE_PDF_BYTES = 10 * 1024 * 1024
GENERIC_ERROR = "the CV service could not handle that right now"

# public_text only redacts the production home directory. Anything else that
# looks like a path is scrubbed too, so a temp file or a container path from an
# upstream error message cannot ride out to a user.
_PATH_RE = re.compile(r"(?:~|/)[A-Za-z0-9._~-]*(?:/[A-Za-z0-9._~-]+)+")


def _error_text(value: object) -> str:
  text = public_text(value, 1000)
  text = _PATH_RE.sub("[path]", text)
  text = text.replace("—", " - ").replace("–", " - ")
  text = re.sub(r"\s+", " ", text).strip()
  return text[:MAX_ERROR_CHARS] or GENERIC_ERROR


class _Parser(argparse.ArgumentParser):
  """An argument parser that never writes to stdout or exits the process.

  The error text is deliberately generic. This CLI is only ever invoked by the
  WhatsApp gate, which relays an "ok": false error straight to the user, so a
  usage string would put the internal subcommand list in a public chat while
  telling that user nothing they can act on. A malformed call is a bug on our
  side, not something the sender can fix by rephrasing.
  """

  def error(self, message: str) -> None:  # type: ignore[override]
    raise CvMakerError(GENERIC_ERROR)

  def exit(self, status: int = 0, message: str | None = None) -> None:  # type: ignore[override]
    raise CvMakerError(GENERIC_ERROR)


def _clean_message(value: object) -> str:
  text = re.sub(r"\s+", " ", str(value or "")).strip()
  if not text:
    raise CvMakerError("there was no message to work with")
  return text[:MAX_MESSAGE_CHARS]


def _summarize(order: dict) -> dict:
  return {
    "orderId": str(order.get("orderId") or ""),
    "phone": str(order.get("phone") or ""),
    "status": str(order.get("status") or ""),
    "updatedAt": str(order.get("updatedAt") or ""),
  }


def _order_for_turn(phone: str, order_id: str) -> dict:
  if order_id:
    order = orders.load_order(order_id)
    if not order:
      raise CvMakerError("that CV order was not found")
    if str(order.get("phone") or "") != phone:
      raise CvMakerError("that CV order belongs to another number")
    return order
  existing = orders.active_order_for(phone)
  if (
    existing
    and str(existing.get("mode") or "") == "interview"
    and str(existing.get("status") or "") == orders.STATUS_INTERVIEWING
  ):
    return existing
  return orders.create_order(phone, "interview")


def cmd_interview_turn(args: argparse.Namespace) -> dict:
  phone = normalize_phone(args.phone)
  message = _clean_message(args.message)
  order = _order_for_turn(phone, str(getattr(args, "order", "") or "").strip())
  order_id = str(order.get("orderId") or "")
  if str(order.get("status") or "") != orders.STATUS_INTERVIEWING:
    raise CvMakerError("that CV order has already moved past the interview")

  # Lazy, in-body import: the interview module is written separately, and this
  # CLI must import and be testable without it.
  from .interview import InterviewUnavailable, run_turn

  try:
    result = run_turn(order, message)
  except InterviewUnavailable as err:
    # The order stays `interviewing`, so the next message picks up where the
    # conversation left off.
    raise CvMakerError(_error_text(err) or "the CV interview is unavailable right now") from err

  payload = result if isinstance(result, dict) else {}
  reply = str(payload.get("reply") or "").strip()
  complete = bool(payload.get("complete"))

  orders.append_turn(order_id, "user", message)
  if reply:
    orders.append_turn(order_id, "assistant", reply)
  if isinstance(payload.get("data"), dict):
    orders.update_order(order_id, data=payload["data"])

  status = orders.STATUS_INTERVIEWING
  if complete:
    status = orders.set_status(order_id, orders.STATUS_AWAITING_PAYMENT)
  return {
    "ok": True,
    "orderId": order_id,
    "status": status,
    "reply": reply,
    "complete": complete,
  }


def cmd_start_revamp(args: argparse.Namespace) -> dict:
  phone = normalize_phone(args.phone)
  raw = str(getattr(args, "cv", "") or "").strip()
  if not raw:
    raise CvMakerError("no CV file was given")
  source = Path(raw).expanduser()
  if not source.is_file():
    raise CvMakerError("that CV file could not be read")
  if source.suffix.casefold() != ".pdf":
    raise CvMakerError("the CV has to be a PDF")
  if source.stat().st_size > MAX_SOURCE_PDF_BYTES:
    raise CvMakerError("that CV file is too large")
  with source.open("rb") as stream:
    if stream.read(5) != b"%PDF-":
      raise CvMakerError("that file is not a readable PDF")

  order = orders.create_order(phone, "revamp")
  order_id = str(order["orderId"])
  target = orders.ensure_order_dir(order_id) / "source.pdf"
  shutil.copyfile(source, target)
  try:
    os.chmod(target, 0o600)
  except OSError:
    pass
  orders.update_order(order_id, sourcePdf=str(target))
  status = orders.set_status(order_id, orders.STATUS_AWAITING_PAYMENT)
  return {"ok": True, "orderId": order_id, "status": status}


def cmd_paid(args: argparse.Namespace) -> dict:
  phone = normalize_phone(args.phone)
  order = orders.active_order_for(phone)
  if not order:
    raise CvMakerError("there is no CV order waiting on this number")
  if str(order.get("status") or "") == orders.STATUS_INTERVIEWING:
    raise CvMakerError("that CV order is still being written")
  order_id = str(order["orderId"])
  return {
    "ok": True,
    "orderId": order_id,
    "status": orders.set_status(order_id, orders.STATUS_AWAITING_APPROVAL),
  }


def cmd_status(args: argparse.Namespace) -> dict:
  """Report the active order for a number, so the gate can resync.

  The gate keeps only a short-lived pointer to which flow a sender is in, and
  that pointer expires well before a payment does. Without a way to read the real
  status back, a buyer who returns after the pointer lapses is treated as a fresh
  interview and can never signal that they paid. This is the read that lets the
  order store stay the source of truth.
  """
  phone = normalize_phone(args.phone)
  order = orders.active_order_for(phone)
  if not order:
    return {"ok": True, "orderId": "", "status": ""}
  return {
    "ok": True,
    "orderId": str(order.get("orderId") or ""),
    "status": str(order.get("status") or ""),
  }


def cmd_approve(args: argparse.Namespace) -> dict:
  order_id = orders.safe_order_id(getattr(args, "order", ""))
  order = orders.load_order(order_id)
  if not order:
    raise CvMakerError("that CV order was not found")
  status = str(order.get("status") or "")
  if status in orders.TERMINAL_STATUSES:
    # Settled orders never re-deliver: report what already happened.
    return {
      "ok": True,
      "orderId": order_id,
      "status": status,
      "phone": str(order.get("phone") or ""),
      "delivered": bool(order.get("delivered")),
      "filename": str(order.get("filename") or ""),
    }
  if status != orders.STATUS_AWAITING_APPROVAL:
    raise CvMakerError("that CV order is not waiting for approval yet")

  result = build.build_and_send(order)
  orders.update_order(
    order_id,
    delivered=result["delivered"],
    filename=result["filename"],
    messageId=result["messageId"],
  )
  # Settling is destructive: it purges the answers, the transcript and the PDF,
  # so the CV can never be rebuilt afterwards. Only a confirmed message id is
  # evidence the document actually reached the buyer. Without one the order stays
  # approvable with its data intact, so the admin can simply try again, rather
  # than the buyer having paid for something that was silently thrown away.
  if not result["delivered"]:
    raise CvMakerError("the CV was built but could not be delivered, try approving again")
  return {
    "ok": True,
    "orderId": order_id,
    "status": orders.set_status(order_id, orders.STATUS_SENT),
    "phone": str(order.get("phone") or ""),
    "delivered": True,
    "filename": result["filename"],
  }


# Sent to the buyer when an admin declines their order. They told us they had
# paid, so silence is the one outcome we must not leave them with. It states the
# outcome without accusing them and leaves the conversation open.
DECLINED_MESSAGE = (
  "Thanks for waiting. We could not confirm your payment, so your CV has not "
  "been sent. Reply here if you think that is wrong and we will take another look."
)


def _notify_declined(phone: str) -> bool:
  """Best effort decline notice. A send failure never fails the admin command.

  The admin has already made the decision and the order is settled by the time
  this runs, so a WhatsApp outage must not make the command look like it failed
  and tempt a second deny.
  """
  target = normalize_phone(phone)
  if not target:
    return False
  try:
    delivery.send_whatsapp_message(target, DECLINED_MESSAGE)
    return True
  except (RuntimeError, OSError):
    return False


def cmd_deny(args: argparse.Namespace) -> dict:
  order_id = orders.safe_order_id(getattr(args, "order", ""))
  order = orders.load_order(order_id)
  if not order:
    raise CvMakerError("that CV order was not found")
  # Read the phone before settling: set_status purges the order's contents.
  phone = str(order.get("phone") or "")
  already_settled = str(order.get("status") or "") in orders.TERMINAL_STATUSES
  status = orders.set_status(order_id, orders.STATUS_REJECTED)
  # Only tell the buyer on the transition, so a repeated deny does not message
  # them twice.
  notified = False if already_settled else _notify_declined(phone)
  return {
    "ok": True,
    "orderId": order_id,
    "status": status,
    "phone": phone,
    "notified": notified,
  }


def cmd_orders(args: argparse.Namespace) -> dict:
  wanted = str(getattr(args, "status", "") or "").strip()
  return {"ok": True, "orders": [_summarize(row) for row in orders.list_orders(wanted or None)]}


def build_parser() -> _Parser:
  parser = _Parser(prog="cv-maker", add_help=False)
  sub = parser.add_subparsers(dest="command")

  turn = sub.add_parser("interview-turn", add_help=False)
  turn.add_argument("--phone", required=True)
  turn.add_argument("--order", default="")
  turn.add_argument("--message", required=True)
  turn.set_defaults(handler=cmd_interview_turn)

  revamp = sub.add_parser("start-revamp", add_help=False)
  revamp.add_argument("--phone", required=True)
  revamp.add_argument("--cv", required=True)
  revamp.set_defaults(handler=cmd_start_revamp)

  paid = sub.add_parser("paid", add_help=False)
  paid.add_argument("--phone", required=True)
  paid.set_defaults(handler=cmd_paid)

  status = sub.add_parser("status", add_help=False)
  status.add_argument("--phone", required=True)
  status.set_defaults(handler=cmd_status)

  approve = sub.add_parser("approve", add_help=False)
  approve.add_argument("--order", required=True)
  approve.set_defaults(handler=cmd_approve)

  deny = sub.add_parser("deny", add_help=False)
  deny.add_argument("--order", required=True)
  deny.set_defaults(handler=cmd_deny)

  listing = sub.add_parser("orders", add_help=False)
  listing.add_argument("--status", default="")
  listing.set_defaults(handler=cmd_orders)

  return parser


def run(argv: list[str]) -> dict:
  parser = build_parser()
  args = parser.parse_args(list(argv))
  handler = getattr(args, "handler", None)
  if handler is None:
    raise CvMakerError(GENERIC_ERROR)
  orders.sweep()
  return handler(args)


def main(argv: list[str] | None = None) -> int:
  try:
    payload = run(list(sys.argv[1:] if argv is None else argv))
  except SystemExit as err:
    payload = {"ok": False, "error": _error_text(err.code if err.code is not None else err)}
  except Exception as err:  # a public user must never see a traceback
    payload = {"ok": False, "error": _error_text(err)}
  print(json.dumps(payload, ensure_ascii=False))
  return 0
