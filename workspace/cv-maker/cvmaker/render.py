"""Render CV HTML to a PDF through the Agent Genaie internal renderer.

POSTs the styled document to `{AGENT_GENAI_BASE_URL}/internal/cv/pdf` and takes
back the bytes, then validates the result locally with Poppler before anything
is delivered. The HTML is untrusted (a model wrote most of it, and a revamp is
built from a file a stranger uploaded), so it goes through Job Scout's
`validate_cv_html_source` first: no scripts, no remote references, no print
chrome. The request body is capped at the same 512 KiB the render worker takes.
"""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import urllib.error
import urllib.request

from jobscout.cv_safety import validate_cv_html_source
from jobscout.delivery import validate_rendered_cv_pdf
from jobscout.integrations.agent_genaie import api_key, endpoint
from jobscout.textutil import public_text

from . import CvMakerError


CV_PDF_PATH = "/internal/cv/pdf"
MAX_CV_HTML_BYTES = 512 * 1024
MAX_CV_PDF_BYTES = 16 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 90


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
