"""Render CV HTML to a PDF through the Agent Genaie internal renderer.

POSTs the styled document to `{AGENT_GENAI_BASE_URL}/internal/cv/pdf` and takes
back the bytes, then validates the result locally with Poppler before anything
is delivered. The HTML is untrusted (a model wrote most of it, and a revamp is
built from a file a stranger uploaded), so it goes through
`validate_cv_html_source` first: no scripts, no remote references, no print
chrome. The request body is capped at the same 512 KiB the render worker takes.
"""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import urllib.error
import urllib.request

import re
import subprocess
import xml.etree.ElementTree as ET

from . import CvMakerError
from .common import api_key, endpoint, public_text
from .cv_html import BROWSER_TIMESTAMP, PAGE_COUNTER, validate_cv_html_source


CV_PDF_PATH = "/internal/cv/pdf"
MAX_CV_HTML_BYTES = 512 * 1024
MAX_CV_PDF_BYTES = 16 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 90


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


def cv_pdf_url() -> str:
  return endpoint(CV_PDF_PATH)


def _write_private_pdf(destination: Path, pdf: bytes) -> Path:
  destination.parent.mkdir(parents=True, exist_ok=True)
  try:
    os.chmod(destination.parent, 0o700)
  except OSError:
    pass
  tmp = destination.with_name(f".{destination.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
  try:
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
      stream.write(pdf)
      stream.flush()
      os.fsync(stream.fileno())
    os.replace(tmp, destination)
    try:
      os.chmod(destination, 0o600)
    except OSError:
      pass
  finally:
    try:
      tmp.unlink()
    except FileNotFoundError:
      pass
  return destination


def render_cv_pdf(html: str, output: str | Path, timeout: int = REQUEST_TIMEOUT_SECONDS) -> Path:
  """Render styled CV HTML and return the validated PDF at `output`."""
  try:
    document = validate_cv_html_source(str(html or ""))
  except RuntimeError as err:
    raise CvMakerError(public_text(err, 240) or "the CV document is not valid") from err

  body = document.encode("utf-8")
  if len(body) > MAX_CV_HTML_BYTES:
    raise CvMakerError("the CV document is too large to render")

  key = str(api_key() or "").strip()
  if not key:
    raise CvMakerError("the CV renderer is not configured")

  request = urllib.request.Request(
    cv_pdf_url(),
    data=body,
    method="POST",
    headers={
      "Content-Type": "text/html; charset=utf-8",
      "Accept": "application/pdf",
      "Authorization": f"Bearer {key}",
    },
  )
  try:
    with urllib.request.urlopen(request, timeout=timeout) as response:
      pdf = response.read()
      content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().casefold()
  except urllib.error.HTTPError as err:
    raise CvMakerError(f"the CV renderer returned HTTP {err.code}") from err
  except (urllib.error.URLError, TimeoutError, OSError) as err:
    raise CvMakerError("the CV renderer is unavailable") from err

  if content_type != "application/pdf" or not pdf.startswith(b"%PDF-"):
    raise CvMakerError("the CV renderer returned an invalid PDF")
  if len(pdf) > MAX_CV_PDF_BYTES:
    raise CvMakerError("the rendered CV is too large")

  destination = _write_private_pdf(Path(output).expanduser(), pdf)
  try:
    validate_rendered_cv_pdf(destination)
  except (RuntimeError, OSError) as err:
    raise CvMakerError(public_text(err, 240) or "the rendered CV did not pass its checks") from err
  return destination
