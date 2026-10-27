"""Turn an approved order into a CV and deliver it.

order -> canonical CV HTML -> styled HTML -> PDF -> WhatsApp document.

Both order modes converge on the same styled document and the same renderer:

  interview   the interview facts go straight into cv_html_from_facts, the same
              builder the web app uses, with no adapter in between
  revamp      the uploaded PDF is read back out into canonical CV HTML by
              convert_pdf_to_cv_html

The rendered PDF is written into the order directory, so settling the order
deletes it along with the rest of the applicant's data.
"""

from __future__ import annotations

import html as html_module
from pathlib import Path
import re

from . import CvMakerError
from .common import document_filename, normalize_phone
from .cv_html import convert_pdf_to_cv_html, cv_html_from_facts, style_cv_html
from . import delivery, orders, render


CV_READY_MESSAGE = "Here is your CV, ready to go. Good luck with your applications!"

_IDENTITY_RE = re.compile(r'data-cv-field="identity"[^>]*>(.*?)</h1>', re.IGNORECASE | re.DOTALL)


def _cv_identity(cv_html: str) -> str:
  """Return the person's name from the semantic CV (the identity <h1>)."""
  match = _IDENTITY_RE.search(str(cv_html or ""))
  if not match:
    return ""
  return html_module.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip()


def _cv_filename(cv_html: str) -> str:
  """Applicant-named PDF filename, e.g. 'Joseph-Munemo-CV.pdf'."""
  return document_filename({"name": _cv_identity(cv_html)}, "cv")


def cv_html_for_order(order: dict, work_dir: Path) -> str:
  """Build the canonical, unstyled CV HTML for this order's mode."""
  mode = str((order or {}).get("mode") or "interview").strip()
  if mode == "revamp":
    source = str((order or {}).get("sourcePdf") or "").strip()
    if not source:
      raise CvMakerError("that order has no uploaded CV to work from")
    path = Path(source).expanduser()
    if not path.is_file():
      raise CvMakerError("the uploaded CV is no longer available")
    return convert_pdf_to_cv_html(path, Path(work_dir) / "cv.html").read_text(encoding="utf-8")
  if mode != "interview":
    raise CvMakerError("that is not a CV order type this service offers")
  facts = (order or {}).get("data")
  if not isinstance(facts, dict) or not str(facts.get("fullName") or "").strip():
    raise CvMakerError("the interview did not finish collecting the details")
  return cv_html_from_facts(facts)


def build_and_send(order: dict) -> dict:
  """Render this order's CV and send it back to the requester.

  The delivery target is normalized exactly like every other phone-taking
  command. Normalization accepts phones and opaque direct WhatsApp LIDs, and
  rejects group JIDs such as "1203...@g.us": a CV carries the
  applicant's name, phone, email and address, and must only ever go back to the
  individual who asked for it.
  """
  phone = normalize_phone((order or {}).get("phone"))
  order_id = orders.safe_order_id((order or {}).get("orderId"))
  work_dir = orders.ensure_order_dir(order_id)

  cv_html = style_cv_html(cv_html_for_order(order, work_dir))
  rendered = render.render_cv_pdf(cv_html, work_dir / _cv_filename(cv_html))
  send_result = delivery.send_whatsapp_document(phone, rendered, CV_READY_MESSAGE, account=order.get("account", "default"))
  message_id = delivery.result_message_id(send_result)
  return {
    "delivered": bool(message_id),
    "filename": rendered.name,
    "messageId": message_id or "",
  }
