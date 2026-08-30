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
import xml.etree.ElementTree as ET

from jobscout import api
from . import config
from .config import env_default
from .commands_setup import convert_pdf_to_cv_html, style_cv_html
from .constants import GENAIE_MAIL_BIN, UNKNOWN_EMPLOYER, WHATSAPP_CHANNEL_SOURCES
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
  return config.render_pdf_url(env_default)


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


MIN_CV_PAGE_FILL_RATIO = 0.85
# Zimbabwean employers accept a CV of up to two pages, so the render pipeline
# allows a second page rather than shrinking every CV onto one.
MAX_CV_PAGES = 2


def pdf_text_fill_ratio(pdf_path: str | Path) -> float:
  """Return the lowest visible word position as a fraction of page height."""
  try:
    completed = subprocess.run(
      ["pdftotext", "-bbox-layout", str(pdf_path), "-"],
      check=True,
      capture_output=True,
      text=True,
      timeout=30,
    )
  except (OSError, subprocess.SubprocessError) as err:
    raise RuntimeError("could not inspect rendered CV page fill") from err
  try:
    document = ET.fromstring(completed.stdout)
    pages = [element for element in document.iter() if element.tag.rsplit("}", 1)[-1] == "page"]
    if len(pages) != 1:
      raise ValueError("expected one page")
    page_height = float(pages[0].attrib["height"])
    word_bottoms = [
      float(element.attrib["yMax"])
      for element in pages[0].iter()
      if element.tag.rsplit("}", 1)[-1] == "word" and str(element.text or "").strip()
    ]
  except (ET.ParseError, KeyError, TypeError, ValueError) as err:
    raise RuntimeError("could not inspect rendered CV page fill") from err
  if page_height <= 0 or not word_bottoms:
    raise RuntimeError("could not inspect rendered CV page fill")
  return max(word_bottoms) / page_height


def validate_rendered_cv_pdf(pdf_path: str | Path) -> Path:
  path = Path(pdf_path)
  pages = pdf_page_count(path)
  if pages > MAX_CV_PAGES:
    raise RuntimeError(f"rendered CV must fit within {MAX_CV_PAGES} pages")
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
  # A one-page CV must not look sparse. A two-page CV fills the first page by
  # construction, so the fill-ratio guard only applies to single-page output.
  if pages == 1:
    fill_ratio = pdf_text_fill_ratio(path)
    if fill_ratio < MIN_CV_PAGE_FILL_RATIO:
      raise RuntimeError(
        f"rendered CV uses only {fill_ratio:.1%} of the page height; "
        f"it must use at least {MIN_CV_PAGE_FILL_RATIO:.1%}"
      )
  return path


# Progressive scale-to-fit steps for the page-limit guarantee. The floor keeps
# the text readable; content that still overflows at the floor is rejected.
_CV_ZOOM_STEPS = (1.0, 0.92, 0.85, 0.78, 0.7)


def _apply_print_zoom(html: str, zoom: float) -> str:
  tag = f"<style>html {{ zoom: {zoom:.2f}; }}</style>"
  head_close = re.search(r"</head\s*>", html, re.IGNORECASE)
  if head_close:
    return html[: head_close.start()] + tag + html[head_close.start() :]
  return tag + html


def render_cv_pdf(html_or_path: str | Path, output: str | Path) -> Path:
  """Render a CV to PDF, scaling it down until it fits within the page limit.

  The modern stylesheet is applied when the source is unstyled (a no-op for an
  AI-designed CV that carries its own <style>). Zimbabwean CVs may run to two
  pages, so a CV is accepted as-is at up to MAX_CV_PAGES; only one that overflows
  that limit is re-rendered at progressively smaller zoom until it fits.
  """
  styled = style_cv_html(validate_cv_html_source(_html_source(html_or_path)))
  pdf = render_html_to_pdf(styled, output)
  for zoom in _CV_ZOOM_STEPS[1:]:
    if pdf_page_count(pdf) <= MAX_CV_PAGES:
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
    return render_cv_pdf(semantic.read_text(encoding="utf-8"), pdf_path.with_name(document_filename(subscriber, "cv")))


_SOURCE_LABELS = {
  "applynow": "on ApplyNow",
  "iharare": "on iHarare Jobs",
  "vacancybox": "on Vacancybox",
  "vacancymail": "on Vacancymail",
}


def _application_source_phrase(candidate: dict) -> str:
  source = str(candidate.get("source") or "").strip().casefold()
  if source in WHATSAPP_CHANNEL_SOURCES or (candidate.get("sourceChannelJid") and candidate.get("sourcePostId")):
    return "in a WhatsApp job advert"
  if source in _SOURCE_LABELS:
    return _SOURCE_LABELS[source]
  raw_url = str(candidate.get("sourcePostUrl") or candidate.get("sourceUrl") or "").strip()
  try:
    parsed = urllib.parse.urlsplit(raw_url)
    host = re.sub(r"^www\.", "", parsed.hostname or "", flags=re.IGNORECASE).strip()
  except ValueError:
    host = ""
  return f"on {host}" if host else ""


def application_email_body(subscriber: dict, candidate: dict) -> str:
  name = display_name(subscriber)
  role = clean_job_title(candidate.get("role")) or "advertised role"
  role_phrase = "the advertised role" if role == "advertised role" else f"the {role} position"
  company = natural_email_text(candidate.get("company"), 120)
  company_phrase = f" at {company}" if company and company.casefold() != UNKNOWN_EMPLOYER.casefold() else ""
  source_phrase = _application_source_phrase(candidate)
  interest = f"I am applying for {role_phrase}{company_phrase}"
  if source_phrase:
    interest = f"{interest}, which I saw {source_phrase}"
  return "\n".join([
    "Dear Hiring Team,",
    "",
    f"My name is {name}. {interest}.",
    "",
    "Please find attached my CV and cover letter for your consideration.",
    "",
    "Kind regards,",
    name,
  ]) + "\n"


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
    # page-limited PDF for the employer.
    attachment = render_cv_pdf(attachment.read_text(encoding="utf-8"), attachment.with_name(document_filename(subscriber, "cv")))
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
    "--body",
    application_email_body(subscriber, candidate),
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
  url = config.web_apply_url(env_default)
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
