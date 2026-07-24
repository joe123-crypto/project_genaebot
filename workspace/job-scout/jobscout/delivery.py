"""Outbound delivery for Job Scout: email, WhatsApp, and the web-apply worker.

Wraps the genaie-mail and openclaw CLIs (via a JSON subprocess helper) and the
website-application worker HTTP call. Imported by dispatch orchestration.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from genaie_env import env_default

from jobscout import api
from .commands_setup import convert_pdf_to_cv_html, style_cv_html
from .constants import DEFAULT_WEB_APPLY_URL, GENAIE_MAIL_BIN
from .cv_safety import BROWSER_TIMESTAMP, PAGE_COUNTER, validate_cv_html_source
from .profile import subscriber_user_id
from .subscriber import display_name, document_filename, subscriber_cv_file, subscriber_sender_email
from .textutil import clean_job_title, display_job_title, natural_email_text, public_text


def parse_json_output(output: str) -> dict:
  try:
    data = json.loads(output or "{}")
  except json.JSONDecodeError:
    return {}
  return data if isinstance(data, dict) else {}


def token_store_key_for_uid(uid: str) -> str:
  digest = hashlib.sha256(str(uid or "").encode("utf-8")).digest()
  encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
  return f"firebase:{encoded}"


def run_json_command(command: list[str], timeout: int = 90, env: dict[str, str] | None = None) -> dict:
  try:
    completed = subprocess.run(
      command,
      check=False,
      capture_output=True,
      text=True,
      timeout=timeout,
      env=env,
    )
  except (OSError, subprocess.TimeoutExpired) as err:
    raise RuntimeError(public_text(err, 240)) from err
  if completed.returncode != 0:
    detail = completed.stderr or completed.stdout or f"exit {completed.returncode}"
    raise RuntimeError(public_text(detail, 240))
  return parse_json_output(completed.stdout)


def gmail_sender_connected(subscriber: dict) -> bool:
  result = run_json_command([str(GENAIE_MAIL_BIN), "senders"], timeout=45)
  senders = result.get("senders")
  if not isinstance(senders, dict):
    return False
  return token_store_key_for_uid(subscriber_user_id(subscriber)) in senders


# The worker rejects larger request bodies, so validation of untrusted child
# output must apply the same cap before a document reaches dispatch.
MAX_RENDER_HTML_BYTES = 512 * 1024


def render_pdf_url() -> str:
  configured = env_default("JOB_SCOUT_RENDER_PDF_URL", "").strip()
  if configured:
    return configured
  apply_url = env_default("JOB_SCOUT_WEB_APPLY_URL", DEFAULT_WEB_APPLY_URL).strip()
  if not apply_url:
    return ""
  return urllib.parse.urljoin(apply_url, "/render-pdf")


def _html_source(value: str | Path) -> str:
  if isinstance(value, Path):
    return value.read_text(encoding="utf-8")
  text = str(value or "")
  if text and "<" not in text:
    # A string without any markup can only be a file path; rendering it as
    # HTML would produce a valid-looking PDF containing just the path text.
    try:
      candidate = Path(text).expanduser()
      if candidate.is_file():
        return candidate.read_text(encoding="utf-8")
    except OSError:
      pass
    raise RuntimeError("HTML source is not a readable file and does not look like HTML")
  return text


def render_html_to_pdf(html_or_path: str | Path, output: str | Path, timeout: int = 90) -> Path:
  """Render bounded raw HTML through the authenticated Playwright worker."""
  url = render_pdf_url()
  token = env_default("JOB_SCOUT_BROWSER_TOKEN", "").strip()
  if not url:
    raise RuntimeError("PDF rendering worker is not configured")
  if not token:
    raise RuntimeError("PDF rendering worker token is not configured")
  html = _html_source(html_or_path)
  body = html.encode("utf-8")
  if not html.strip():
    raise RuntimeError("HTML document is empty")
  if len(body) > MAX_RENDER_HTML_BYTES:
    raise RuntimeError("HTML document is too large")
  request = urllib.request.Request(
    url,
    data=body,
    method="POST",
    headers={
      "Content-Type": "text/html; charset=utf-8",
      "Accept": "application/pdf",
      "Authorization": f"Bearer {token}",
    },
  )
  try:
    with urllib.request.urlopen(request, timeout=timeout) as response:
      pdf = response.read()
      content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().casefold()
  except urllib.error.HTTPError as err:
    raise RuntimeError(f"PDF rendering worker returned HTTP {err.code}") from err
  except (urllib.error.URLError, TimeoutError, OSError) as err:
    raise RuntimeError("PDF rendering worker is unavailable") from err
  if content_type != "application/pdf" or not pdf.startswith(b"%PDF-"):
    raise RuntimeError("PDF rendering worker returned an invalid PDF")
  if len(pdf) > 16 * 1024 * 1024:
    raise RuntimeError("rendered PDF is too large")
  destination = Path(output).expanduser()
  destination.parent.mkdir(parents=True, exist_ok=True)
  temporary = destination.with_name(f".{destination.name}.tmp")
  temporary.write_bytes(pdf)
  temporary.replace(destination)
  return destination


def pdf_page_count(pdf_path: str | Path) -> int:
  """Return a verified positive PDF page count, failing closed on inspection errors."""
  try:
    completed = subprocess.run(
      ["pdfinfo", str(pdf_path)], check=True, capture_output=True, text=True, timeout=30,
    )
  except (OSError, subprocess.SubprocessError) as err:
    raise RuntimeError("could not verify rendered CV page count") from err
  match = re.search(r"^Pages:\s*(\d+)", completed.stdout, re.MULTILINE)
  if not match or int(match.group(1)) <= 0:
    raise RuntimeError("could not verify rendered CV page count")
  return int(match.group(1))


def validate_rendered_cv_pdf(pdf_path: str | Path) -> Path:
  path = Path(pdf_path)
  if pdf_page_count(path) != 1:
    raise RuntimeError("rendered CV must fit on exactly one page")
  try:
    completed = subprocess.run(
      ["pdftotext", str(path), "-"], check=True, capture_output=True, text=True, timeout=30,
    )
  except (OSError, subprocess.SubprocessError) as err:
    raise RuntimeError("could not inspect rendered CV text") from err
  lines = [re.sub(r"\s+", " ", line).strip() for line in completed.stdout.replace("\f", "\n").splitlines() if line.strip()]
  if any(line.casefold().startswith(("file://", "about:blank")) for line in lines):
    raise RuntimeError("rendered CV contains browser print chrome")
  if any(BROWSER_TIMESTAMP.fullmatch(line) or PAGE_COUNTER.fullmatch(line) for line in lines):
    raise RuntimeError("rendered CV contains browser print chrome")
  return path


# Progressive scale-to-fit steps for the one-page guarantee. The floor keeps the
# text readable; content that still overflows at the floor is rejected.
_CV_ZOOM_STEPS = (1.0, 0.92, 0.85, 0.78, 0.7)


def _apply_print_zoom(html: str, zoom: float) -> str:
  tag = f"<style>html {{ zoom: {zoom:.2f}; }}</style>"
  head_close = re.search(r"</head\s*>", html, re.IGNORECASE)
  if head_close:
    return html[: head_close.start()] + tag + html[head_close.start() :]
  return tag + html


def render_one_page_cv(html_or_path: str | Path, output: str | Path) -> Path:
  """Render a CV to PDF, scaling it down until it fits on a single page.

  The modern stylesheet is applied when the source is unstyled (a no-op for an
  AI-designed CV that carries its own <style>). If the CV overflows one page it
  is re-rendered at progressively smaller zoom, so a long CV shrinks to one page
  rather than spilling onto a second.
  """
  styled = style_cv_html(validate_cv_html_source(_html_source(html_or_path)))
  pdf = render_html_to_pdf(styled, output)
  for zoom in _CV_ZOOM_STEPS[1:]:
    if pdf_page_count(pdf) <= 1:
      break
    pdf = render_html_to_pdf(_apply_print_zoom(styled, zoom), output)
  return validate_rendered_cv_pdf(pdf)


def cover_letter_html(text: str) -> str:
  raw_text = str(text or "").strip()
  paragraphs = [block.strip() for block in raw_text.split("\n\n") if block.strip()]
  nonempty_lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
  if not paragraphs or not nonempty_lines:
    raise RuntimeError("cover letter text is missing")
  applicant = nonempty_lines[-1]
  body = "\n".join(
    f"      <p>{html.escape(paragraph).replace(chr(10), '<br>')}</p>"
    for paragraph in paragraphs
  )
  return (
    "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
    f"<title>{html.escape(applicant)} - Cover Letter</title><style>"
    "@page{size:A4;margin:0}*{box-sizing:border-box}html,body{margin:0;padding:0;background:#fff;color:#202b3c;"
    "font-family:Arial,Helvetica,sans-serif;font-size:11pt;line-height:1.6}main{width:210mm;min-height:297mm;"
    "padding:20mm 21mm}header{padding-bottom:7mm;border-bottom:2px solid #175ec8;margin-bottom:16mm}"
    "h1{margin:0;color:#101826;font-size:22pt;line-height:1.15}header p{margin:2mm 0 0;color:#405a78;"
    "font-size:9pt;text-transform:uppercase}article{max-width:165mm}article p{margin:0 0 6mm;white-space:pre-line}"
    "</style></head><body><main><header>"
    f"<h1>{html.escape(applicant)}</h1><p>Application Letter</p></header><article>\n{body}\n"
    "</article></main></body></html>"
  )


def _rebuild_stored_pdf_cv(pdf_path: Path, subscriber: dict) -> Path:
  """Re-extract and re-render a legacy stored PDF CV into a clean, styled PDF.

  Strips any browser print chrome (the `file://` leak, timestamps, page counters
  are dropped during extraction) and applies the current template, so a stale
  print-to-PDF artifact never reaches an employer verbatim.
  """
  with tempfile.TemporaryDirectory() as tmp:
    semantic = convert_pdf_to_cv_html(pdf_path, Path(tmp) / "cv.html")
    return render_one_page_cv(semantic.read_text(encoding="utf-8"), pdf_path.with_name(document_filename(subscriber, "cv")))


def send_application_email(
  subscriber: dict,
  candidate: dict,
  cover_letter: Path,
  cv_pdf: Path | None = None,
  cover_letter_pdf: Path | None = None,
) -> dict:
  email = str(candidate.get("applicationEmail") or "").strip()
  if not email:
    raise RuntimeError("candidate is missing applicationEmail")
  role = display_job_title(candidate) or clean_job_title(candidate.get("role")) or "advertised role"
  subject = natural_email_text(f"Application for {role} - {display_name(subscriber)}", 180)
  attachment = Path(cv_pdf) if cv_pdf is not None else Path(subscriber_cv_file(subscriber))
  if attachment.suffix.casefold() != ".pdf":
    # The stored base CV is unstyled semantic HTML; style it and render to a
    # single-page PDF for the employer.
    attachment = render_one_page_cv(attachment.read_text(encoding="utf-8"), attachment.with_name(document_filename(subscriber, "cv")))
  elif cv_pdf is None:
    # A stored legacy PDF must never be emailed verbatim: it may be a browser
    # "print to PDF" artifact carrying print chrome (timestamps, page counters,
    # and a file:// path that leaks the applicant's local filesystem). Rebuild it
    # into clean, styled HTML first. cv_pdf supplied by the caller is already a
    # rendered artifact from our pipeline, so it is trusted as-is.
    attachment = _rebuild_stored_pdf_cv(attachment, subscriber)
  letter_attachment = Path(cover_letter_pdf) if cover_letter_pdf is not None else Path(cover_letter).with_name(document_filename(subscriber, "cover_letter"))
  if cover_letter_pdf is None:
    letter_text = Path(cover_letter).read_text(encoding="utf-8")
    letter_attachment = render_html_to_pdf(cover_letter_html(letter_text), letter_attachment)
  return run_json_command([
    str(GENAIE_MAIL_BIN),
    "send",
    "--from",
    subscriber_sender_email(subscriber),
    "--sender-uid",
    subscriber_user_id(subscriber),
    "--to",
    email,
    "--subject",
    subject,
    "--body-file",
    str(cover_letter),
    "--attach",
    str(attachment),
    "--attach",
    str(letter_attachment),
    "--force",
  ])


def send_whatsapp_message(target: str, message: str) -> dict:
  command_env = os.environ.copy()
  command_env.pop("OPENCLAW_HOME", None)
  gateway_token = command_env.get("GATEWAY_AUTH_TOKEN", "").strip()
  if gateway_token:
    command_env["OPENCLAW_GATEWAY_TOKEN"] = gateway_token
  return run_json_command([
    str(api.OPENCLAW_BIN),
    "message",
    "send",
    "--channel",
    "whatsapp",
    "--target",
    target,
    "--message",
    message,
    "--json",
  ], env=command_env)


def send_whatsapp_summary(phone: str, message: str) -> dict:
  return send_whatsapp_message(phone, message)


def result_message_id(result: dict) -> str | None:
  for key in ("messageId", "id", "gmailMessageId"):
    value = str(result.get(key) or "").strip()
    if value:
      return value
  nested = result.get("message")
  if isinstance(nested, dict):
    for key in ("id", "messageId"):
      value = str(nested.get(key) or "").strip()
      if value:
        return value
  return None


def request_web_application(payload: dict, timeout: int = 180) -> dict:
  url = env_default("JOB_SCOUT_WEB_APPLY_URL", DEFAULT_WEB_APPLY_URL).strip()
  token = env_default("JOB_SCOUT_BROWSER_TOKEN", "").strip()
  if not url:
    raise RuntimeError("website application worker is not configured")
  if not token:
    raise RuntimeError("website application worker token is not configured")
  request = urllib.request.Request(
    url,
    data=json.dumps(payload).encode("utf-8"),
    method="POST",
    headers={
      "Content-Type": "application/json",
      "Accept": "application/json",
      "Authorization": f"Bearer {token}",
    },
  )
  try:
    with urllib.request.urlopen(request, timeout=timeout) as response:
      data = json.loads(response.read().decode("utf-8") or "{}")
  except urllib.error.HTTPError as err:
    raise RuntimeError(f"website application worker returned HTTP {err.code}") from err
  except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as err:
    raise RuntimeError("website application worker is unavailable") from err
  if not isinstance(data, dict) or data.get("outcome") not in {"submitted", "action_required", "failed"}:
    raise RuntimeError("website application worker returned an invalid result")
  return data
