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
from .util import normalize_phone, phone_hash, utc_now


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
  data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
  request = urllib.request.Request(
    url,
    data=data,
    method=method,
    headers={
      "Authorization": f"Bearer {key}",
      "Content-Type": "application/json",
    },
  )
  try:
    with urllib.request.urlopen(request, timeout=45) as response:
      body = response.read().decode("utf-8")
      return json.loads(body) if body else {}
  except urllib.error.HTTPError as err:
    body = err.read().decode("utf-8", errors="replace")
    print(f"Agent Genaie Job Scout API failed with HTTP {err.code}: {body}", file=sys.stderr)
    raise SystemExit(1) from err
  except urllib.error.URLError as err:
    print(f"Could not reach Agent Genaie Job Scout API: {err}", file=sys.stderr)
    raise SystemExit(1) from err


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
    raw = err.read().decode("utf-8", errors="replace")
    print(f"Agent Genaie Job Scout API failed with HTTP {err.code}: {raw}", file=sys.stderr)
    raise SystemExit(1) from err
  except urllib.error.URLError as err:
    print(f"Could not reach Agent Genaie Job Scout API: {err}", file=sys.stderr)
    raise SystemExit(1) from err


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


def download_cv(user_id: str, dest: Path) -> Path:
  """Fetch a presigned CV URL from agent-genaie and download it to dest."""
  user = str(user_id or "").strip()
  if not user:
    raise SystemExit("user-id is required to download a CV.")
  query = urllib.parse.urlencode({"userId": user})
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


def cv_object_available(user_id: str) -> bool:
  """Non-fatal readiness probe: does the user's CV actually exist in R2?

  The GET endpoint returns 404 when there is no cvFileRef or the object is
  missing, so a 200 with a URL means the CV is truly downloadable.
  """
  user = str(user_id or "").strip()
  key = api_key()
  if not user or not key:
    return False
  url = endpoint(f"/internal/job-scout/cv?{urllib.parse.urlencode({'userId': user})}")
  request = urllib.request.Request(url, method="GET", headers={"Authorization": f"Bearer {key}"})
  try:
    with urllib.request.urlopen(request, timeout=30) as response:
      data = json.loads(response.read().decode("utf-8") or "{}")
  except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
    return False
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
