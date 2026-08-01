"""Agent Genaie internal API client and CV storage for Job Scout.

The authenticated JSON/multipart HTTP client, CV upload/download against the R2
store, account-link lookups, and OpenClaw binary resolution. Network-facing;
imported by the CLI and by the dispatch/subscriber layers.
"""

from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from genaie_env import api_key, endpoint

from .constants import (
  CV_URL_LOOKUP_ATTEMPTS,
  CV_URL_LOOKUP_RETRY_SECONDS,
  OPENCLAW_BIN_FALLBACKS,
)
from .textutil import public_text
from .util import normalize_phone, phone_hash, utc_now


SAFE_READ_ATTEMPTS = 3
SAFE_READ_RETRY_SECONDS = 1
TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


class JobScoutApiError(RuntimeError):
  """Actionable, sanitized failure from the Agent Genaie internal API."""

  def __init__(self, message: str, *, status_code: int | None = None) -> None:
    super().__init__(message)
    self.status_code = status_code


def _http_error_message(status_code: int, body: str) -> str:
  detail: object = body
  try:
    parsed = json.loads(body) if body else None
  except json.JSONDecodeError:
    parsed = None
  if isinstance(parsed, dict):
    detail = parsed.get("error") or parsed.get("message") or ""
  safe_detail = public_text(detail, 240)
  suffix = f": {safe_detail}" if safe_detail else ""
  return f"Agent Genaie Job Scout API failed with HTTP {status_code}{suffix}"


def _read_http_error_body(error: urllib.error.HTTPError) -> str:
  try:
    raw = error.read()
  except (AttributeError, OSError):
    return ""
  if isinstance(raw, bytes):
    return raw.decode("utf-8", errors="replace")
  return str(raw or "")


def _network_error_message(error: BaseException) -> str:
  detail = public_text(getattr(error, "reason", error), 240)
  suffix = f": {detail}" if detail else ""
  return f"Could not reach Agent Genaie Job Scout API{suffix}"


def resolve_openclaw_bin(fallbacks: tuple[Path, ...] = OPENCLAW_BIN_FALLBACKS) -> Path:
  configured = os.environ.get("OPENCLAW_BIN", "").strip()
  if configured:
    return Path(configured)

  discovered = shutil.which("openclaw")
  if discovered:
    return Path(discovered)

  for candidate in fallbacks:
    if candidate.is_file() and os.access(candidate, os.X_OK):
      return candidate

  return Path("openclaw")


OPENCLAW_BIN = resolve_openclaw_bin()


def pending_links_path() -> Path:
  return Path(
    os.environ.get(
      "OPENCLAW_PENDING_LINKS_PATH",
      "/home/joseph/.openclaw/runtime/account-link/pending-links.json",
    )
  ).expanduser()


def request_json(method: str, url: str, payload: dict | None = None) -> dict:
  key = api_key()
  if not key:
    raise SystemExit("Missing AGENT_GENAI_INTERNAL_API_KEY.")
  normalized_method = str(method or "GET").upper()
  data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
  request = urllib.request.Request(
    url,
    data=data,
    method=normalized_method,
    headers={
      "Authorization": f"Bearer {key}",
      "Content-Type": "application/json",
    },
  )
  attempts = SAFE_READ_ATTEMPTS if normalized_method == "GET" else 1
  for attempt in range(attempts):
    try:
      with urllib.request.urlopen(request, timeout=45) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else {}
    except urllib.error.HTTPError as err:
      message = _http_error_message(err.code, _read_http_error_body(err))
      should_retry = normalized_method == "GET" and err.code in TRANSIENT_HTTP_STATUSES and attempt + 1 < attempts
      if should_retry:
        print(f"{message}; retrying safe read ({attempt + 2}/{attempts})", file=sys.stderr)
        time.sleep(SAFE_READ_RETRY_SECONDS * (attempt + 1))
        continue
      print(message, file=sys.stderr)
      raise JobScoutApiError(message, status_code=err.code) from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
      message = _network_error_message(err)
      if normalized_method == "GET" and attempt + 1 < attempts:
        print(f"{message}; retrying safe read ({attempt + 2}/{attempts})", file=sys.stderr)
        time.sleep(SAFE_READ_RETRY_SECONDS * (attempt + 1))
        continue
      print(message, file=sys.stderr)
      raise JobScoutApiError(message) from err
  raise AssertionError("Agent Genaie API retry loop ended unexpectedly")


def request_multipart(url: str, fields: dict[str, str], file_field: str, file_path: Path, filename: str, content_type: str) -> dict:
  key = api_key()
  if not key:
    raise SystemExit("Missing AGENT_GENAI_INTERNAL_API_KEY.")
  boundary = "----genaebot" + os.urandom(16).hex()
  chunks: list[bytes] = []
  for name, value in fields.items():
    if value is None:
      continue
    chunks.append(f"--{boundary}\r\n".encode("utf-8"))
    chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
    chunks.append(f"{value}\r\n".encode("utf-8"))
  chunks.append(f"--{boundary}\r\n".encode("utf-8"))
  chunks.append(f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode("utf-8"))
  chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
  chunks.append(Path(file_path).read_bytes())
  chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
  body = b"".join(chunks)
  request = urllib.request.Request(
    url,
    data=body,
    method="POST",
    headers={
      "Authorization": f"Bearer {key}",
      "Content-Type": f"multipart/form-data; boundary={boundary}",
    },
  )
  try:
    with urllib.request.urlopen(request, timeout=60) as response:
      raw = response.read().decode("utf-8")
      return json.loads(raw) if raw else {}
  except urllib.error.HTTPError as err:
    message = _http_error_message(err.code, _read_http_error_body(err))
    print(message, file=sys.stderr)
    raise SystemExit(message) from err
  except (urllib.error.URLError, TimeoutError, OSError) as err:
    message = _network_error_message(err)
    print(message, file=sys.stderr)
    raise SystemExit(message) from err


def safe_cv_suffix(path: Path) -> str:
  suffix = path.suffix.lower()
  allowed = {".pdf", ".doc", ".docx", ".txt"}
  if suffix not in allowed:
    guessed = mimetypes.guess_type(path.name)[0] or ""
    if guessed == "application/pdf":
      return ".pdf"
    raise SystemExit("CV must be a PDF, DOC, DOCX, or TXT file for v1.")
  return suffix


def upload_cv(user_id: str, source: str | Path) -> dict:
  """Upload a CV PDF to the agent-genaie R2 bucket and return {ok, key}."""
  src = Path(source).expanduser().resolve()
  if not src.exists() or not src.is_file():
    raise SystemExit(f"CV file not found: {src}")
  if safe_cv_suffix(src) != ".pdf":
    raise SystemExit("CV must be a PDF file (the R2 store accepts application/pdf only).")
  user = str(user_id or "").strip()
  if not user:
    raise SystemExit("user-id is required to upload a CV.")
  return request_multipart(
    endpoint("/internal/job-scout/cv"),
    {"userId": user},
    "file",
    src,
    "cv.pdf",
    "application/pdf",
  )


def upload_cv_html(user_id: str, source: str | Path) -> dict:
  """Upload a manually verified canonical HTML CV."""
  src = Path(source).expanduser().resolve()
  if not src.exists() or not src.is_file():
    raise SystemExit(f"CV HTML file not found: {src}")
  user = str(user_id or "").strip()
  if not user:
    raise SystemExit("user-id is required to upload CV HTML.")
  return request_multipart(
    endpoint("/internal/job-scout/cv"),
    {"userId": user, "format": "html"},
    "file",
    src,
    "cv.html",
    "text/html",
  )


def download_cv(user_id: str, dest: Path, format: str = "html") -> Path:
  """Fetch the subscriber's canonical CV object and download it to ``dest``.

  Agent Genaie's CV route now resolves the ready HTML object by default. The
  helper deliberately follows the route response rather than constructing an
  R2 key so legacy profiles and migrated profiles keep using one seam.
  """
  user = str(user_id or "").strip()
  if not user:
    raise SystemExit("user-id is required to download a CV.")
  query = urllib.parse.urlencode({"userId": user, "format": format})
  url = ""
  for attempt in range(CV_URL_LOOKUP_ATTEMPTS):
    result = request_json("GET", endpoint(f"/internal/job-scout/cv?{query}"))
    url = str(result.get("url") or "").strip()
    if url:
      break
    if attempt + 1 < CV_URL_LOOKUP_ATTEMPTS:
      time.sleep(CV_URL_LOOKUP_RETRY_SECONDS)
  if not url:
    raise RuntimeError("subscriber CV is unavailable; re-upload is required")
  request = urllib.request.Request(url, method="GET", headers={"Accept": "*/*"})
  try:
    with urllib.request.urlopen(request, timeout=60) as response:
      data = response.read()
  except (urllib.error.URLError, TimeoutError, OSError) as err:
    raise RuntimeError("could not download subscriber CV from storage") from err
  dest.parent.mkdir(parents=True, exist_ok=True)
  dest.write_bytes(data)
  return dest


_APPLICATION_ARTIFACTS = {
  "cv": ("cv.pdf", "application/pdf"),
  "cover_letter_text": ("cover-letter.txt", "text/plain; charset=utf-8"),
  "cover_letter_pdf": ("cover-letter.pdf", "application/pdf"),
}


def upload_application_artifact(
  user_id: str,
  application_id: str,
  attempt: int,
  kind: str,
  source: str | Path,
) -> dict:
  """Store an exact submitted application artifact in Agent Genaie's R2.

  The API owns key construction and verifies that ``application_id`` belongs
  to ``user_id``. Keeping clients away from arbitrary object keys prevents a
  compromised agent output from crossing a user's storage prefix.
  """
  user = str(user_id or "").strip()
  application = str(application_id or "").strip()
  artifact_kind = str(kind or "").strip()
  if not user:
    raise RuntimeError("user id is required to upload an application artifact")
  if not application or len(application) > 160:
    raise RuntimeError("application id is required to upload an application artifact")
  if artifact_kind not in _APPLICATION_ARTIFACTS:
    raise RuntimeError("unsupported application artifact kind")
  try:
    attempt_number = int(attempt)
  except (TypeError, ValueError) as err:
    raise RuntimeError("application attempt must be a positive integer") from err
  if attempt_number < 1 or attempt_number > 99:
    raise RuntimeError("application attempt must be between 1 and 99")
  path = Path(source).expanduser().resolve()
  if not path.is_file():
    raise RuntimeError("application artifact file is missing")
  filename, content_type = _APPLICATION_ARTIFACTS[artifact_kind]
  data = path.read_bytes()
  if not data:
    raise RuntimeError("application artifact file is empty")
  if artifact_kind in {"cv", "cover_letter_pdf"} and not data.startswith(b"%PDF-"):
    raise RuntimeError("application PDF artifact is invalid")
  try:
    return request_multipart(
      endpoint("/internal/job-scout/application-artifact"),
      {
        "userId": user,
        "applicationId": application,
        "attempt": str(attempt_number),
        "kind": artifact_kind,
      },
      "file",
      path,
      filename,
      content_type,
    )
  except SystemExit as err:
    # Artifact retention runs after the external application has succeeded.
    # Surface a normal exception so the orchestrator can record/alert without
    # terminating the whole parent run or retrying the submission.
    raise RuntimeError("could not store the submitted application artifact") from err


def cv_object_available(user_id: str) -> bool:
  """Readiness probe: does the user's CV actually exist in R2?

  The GET endpoint returns 404 when there is no cvFileRef or the object is
  missing, so a 200 with a URL means the CV is truly downloadable. Transient
  service failures are retried and then propagated so they are not mislabeled
  as a missing user CV.
  """
  user = str(user_id or "").strip()
  key = api_key()
  if not user or not key:
    return False
  url = endpoint(f"/internal/job-scout/cv?{urllib.parse.urlencode({'userId': user, 'format': 'html'})}")
  try:
    data = request_json("GET", url)
  except JobScoutApiError as err:
    if err.status_code == 404:
      return False
    raise
  return bool(str(data.get("url") or "").strip())


def linked_user_id(phone: str) -> str:
  query = urllib.parse.urlencode({"phone": normalize_phone(phone)})
  result = request_json("GET", endpoint(f"/internal/account-link/status?{query}"))
  user_id = str(result.get("userId") or "").strip()
  if not result.get("linked") or not user_id:
    raise SystemExit("phone is not linked to an Agent Genaie user")
  return user_id


def stage_cv(phone: str, source: str) -> dict:
  normalized_phone = normalize_phone(phone)
  src = Path(source).expanduser().resolve()
  user_id = linked_user_id(normalized_phone)
  result = upload_cv(user_id, src)
  return {
    "ok": True,
    "phoneHash": phone_hash(normalized_phone),
    "userId": user_id,
    "cvFileRef": result.get("key"),
    "originalPath": str(src),
    "storedAt": utc_now(),
  }
