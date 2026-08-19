"""Standalone CV maker: build or revamp a CV and send it to the user on WhatsApp.

This is the engine behind the standalone "CV Maker" public service, for people
who just want a professional CV without enrolling in Job Scout. It reuses the
exact Job Scout CV pipeline and adds nothing to the subscriber/dispatch
machinery: no account link, no Gmail, no R2, no Firestore, no stored profile.

Two inputs, both converging on the same canonical-HTML construction and the same
one-page renderer as Job Scout:

  --cv <pdf>            revamp an uploaded CV (convert_pdf_to_cv_html)
  --details-file <txt>  build from scratch from agent-collected canonical text
                        (name on line 1, blank line, then SECTION headings each
                        followed by their content lines) -> cv_html_document

The finished, validated one-page PDF is delivered straight back to --phone over
WhatsApp via send_whatsapp_document.
"""

from __future__ import annotations

import argparse
import html as html_module
import re
import tempfile
from pathlib import Path

from . import delivery
from .commands_setup import convert_pdf_to_cv_html, cv_html_document
from .subscriber import document_filename

CV_READY_MESSAGE = "Here is your CV, ready to go. Good luck with your applications!"


def _cv_identity(cv_html: str) -> str:
  """Return the person's name from the semantic CV (the identity <h1>)."""
  match = re.search(
    r'data-cv-field="identity"[^>]*>(.*?)</h1>', cv_html, re.IGNORECASE | re.DOTALL
  )
  if not match:
    return ""
  return html_module.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip()


def _cv_filename(cv_html: str) -> str:
  """Applicant-named PDF filename, e.g. 'Joseph-Munemo-CV.pdf'."""
  return document_filename({"name": _cv_identity(cv_html)}, "cv")


def cmd_make_cv(args: argparse.Namespace) -> dict:
  phone = str(getattr(args, "phone", "") or "").strip()
  if not phone:
    raise SystemExit("--phone is required")
  cv_path = str(getattr(args, "cv", "") or "").strip()
  details_path = str(getattr(args, "details_file", "") or "").strip()
  if bool(cv_path) == bool(details_path):
    raise SystemExit("provide exactly one of --cv or --details-file")

  with tempfile.TemporaryDirectory(prefix="cv-maker-") as tmp:
    tmp_dir = Path(tmp)
    if cv_path:
      source = Path(cv_path).expanduser()
      if not source.is_file():
        raise SystemExit("--cv file does not exist")
      cv_html = convert_pdf_to_cv_html(source, tmp_dir / "cv.html").read_text(encoding="utf-8")
      mode = "revamp"
    else:
      source = Path(details_path).expanduser()
      if not source.is_file():
        raise SystemExit("--details-file does not exist")
      cv_html = cv_html_document(source.read_text(encoding="utf-8"))
      mode = "details"

    rendered = delivery.render_one_page_cv(cv_html, tmp_dir / _cv_filename(cv_html))
    pages = delivery.pdf_page_count(rendered)
    send_result = delivery.send_whatsapp_document(phone, rendered, CV_READY_MESSAGE)
    message_id = delivery.result_message_id(send_result)
    filename = rendered.name

  return {
    "ok": True,
    "mode": mode,
    "delivered": bool(message_id),
    "pages": pages,
    "filename": filename,
    "messageId": message_id,
  }
