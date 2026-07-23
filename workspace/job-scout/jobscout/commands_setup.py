"""CLI commands for Job Scout setup, status, profiles, and candidate previews."""

from __future__ import annotations

import argparse
import hashlib
import html as html_module
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from genaie_env import agent_base_url, agent_base_url_source, api_key, endpoint, env_default
from pending_cache import (
  remove_pending_links,
)

from . import api
from .constants import DEFAULT_JSEARCH_COUNTRY, DEFAULT_JSEARCH_LANGUAGE, DEFAULT_WEB_APPLY_URL, RUNTIME_ROOT
from .cv_safety import validate_cv_html_source, visible_lines_from_html
from .jsearch import fetch_jsearch_candidates, fetch_scraped_candidates, fetch_zimlinks_candidates, filter_candidates, jsearch_provider
from .profile import (
  clean_items,
  load_profile_json,
  preference_code,
  profile_values,
  resolve_preferred_locations,
  resolve_profile_locations,
  search_queries,
  subscriber_user_id,
  user_key,
)
from .subscriber import subscriber_cv_html_ready
from .textutil import public_text
from .util import effective_user, normalize_phone, phone_hash, utc_now


def _cv_facts(source: str) -> tuple[set[str], set[str], set[str]]:
  visible = " ".join(visible_lines_from_html(source))
  emails = {value.casefold() for value in re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", visible)}
  phones = {
    digits[-9:]
    for value in re.findall(r"\+?\d[\d\s()\-]{6,}\d", visible)
    if len(digits := re.sub(r"\D", "", value)) >= 9
  }
  years = set(re.findall(r"\b(?:19|20)\d{2}\b", visible))
  return emails, phones, years


def cmd_repair_cv(args: argparse.Namespace) -> dict:
  """Safely replace one ready canonical CV, with a private reversible backup."""
  target = normalize_phone(args.phone)
  source_path = Path(args.source_html).expanduser().resolve()
  if not source_path.is_file():
    raise SystemExit("source HTML file does not exist")
  source_html = validate_cv_html_source(source_path.read_text(encoding="utf-8"))
  ready = cmd_list_ready(argparse.Namespace(limit=1000)).get("subscribers", [])
  matches = [
    subscriber for subscriber in ready
    if normalize_phone(str(subscriber.get("whatsappPhone") or "")) == target
  ]
  if len(matches) != 1:
    raise SystemExit("expected exactly one ready Job Scout subscriber for that phone")
  subscriber = matches[0]
  user_id = subscriber_user_id(subscriber)
  stamp = utc_now().replace(":", "").replace("-", "")
  backup_root = Path(args.backup_dir).expanduser() if args.backup_dir else RUNTIME_ROOT / "cv-repair" / stamp
  backup_dir = backup_root / phone_hash(target)
  backup_dir.mkdir(parents=True, exist_ok=True)
  os.chmod(backup_root, 0o700)
  os.chmod(backup_dir, 0o700)
  before_path = api.download_cv(user_id, backup_dir / "canonical-before.html")
  os.chmod(before_path, 0o600)
  before_html = before_path.read_text(encoding="utf-8")
  before_facts = _cv_facts(before_html)
  after_facts = _cv_facts(source_html)
  labels = ("email", "phone", "year")
  missing = {
    label: sorted(existing - replacement)
    for label, existing, replacement in zip(labels, before_facts, after_facts)
    if existing - replacement
  }
  display = str((subscriber.get("profile") or {}).get("displayName") or subscriber.get("displayName") or "").strip()
  source_text = " ".join(visible_lines_from_html(source_html)).casefold()
  if display and not all(token.casefold() in source_text for token in re.findall(r"\w+", display) if len(token) > 1):
    raise SystemExit("replacement CV does not preserve the subscriber display name")
  if missing:
    raise SystemExit(f"replacement CV drops canonical facts: {', '.join(sorted(missing))}")
  before_sha = hashlib.sha256(before_path.read_bytes()).hexdigest()
  source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
  manifest = {
    "schemaVersion": 1,
    "createdAt": utc_now(),
    "userKey": user_key(user_id),
    "beforeSha256": before_sha,
    "replacementSha256": source_sha,
    "applied": bool(args.apply),
  }
  manifest_path = backup_dir / "manifest.json"
  manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  os.chmod(manifest_path, 0o600)
  if not args.apply:
    return {"ok": True, "dryRun": True, "userKey": user_key(user_id), "backupDirectory": str(backup_dir), **manifest}
  api.upload_cv_html(user_id, source_path)
  after_path = api.download_cv(user_id, backup_dir / "canonical-after.html")
  os.chmod(after_path, 0o600)
  after_sha = hashlib.sha256(after_path.read_bytes()).hexdigest()
  if after_sha != source_sha:
    raise RuntimeError("canonical CV verification hash does not match the replacement")
  manifest.update({"applied": True, "verifiedSha256": after_sha, "verifiedAt": utc_now()})
  manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  return {"ok": True, "dryRun": False, "userKey": user_key(user_id), "backupDirectory": str(backup_dir), **manifest}

def cmd_check_runtime(args: argparse.Namespace) -> dict:
  cache_path = api.pending_links_path()
  cache_parent = cache_path.parent
  probe = cache_parent / f".{cache_path.name}.{os.getpid()}.check"
  tmp = cache_parent / f".{cache_path.name}.{os.getpid()}.tmp"
  errors: list[str] = []

  if not cache_parent.exists():
    errors.append(f"pending-link cache parent does not exist: {cache_parent}")
  elif not cache_parent.is_dir():
    errors.append(f"pending-link cache parent is not a directory: {cache_parent}")
  else:
    try:
      probe.write_text("job-scout runtime check\n", encoding="utf-8")
      os.chmod(probe, 0o600)
      tmp.write_text(json.dumps({"ok": True, "checkedAt": utc_now()}) + "\n", encoding="utf-8")
      os.chmod(tmp, 0o600)
      tmp.replace(probe)
    except OSError as err:
      errors.append(f"cannot create and replace a temp file in pending-link cache parent: {err}")
    finally:
      for path in (tmp, probe):
        try:
          path.unlink()
        except FileNotFoundError:
          pass
        except OSError:
          pass

  has_api_key = bool(api_key())
  if not has_api_key:
    errors.append("Missing AGENT_GENAI_INTERNAL_API_KEY.")

  runtime_probe = RUNTIME_ROOT / f".runtime-{os.getpid()}.check"
  try:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    runtime_probe.write_text("ok\n", encoding="utf-8")
  except OSError as err:
    errors.append(f"Job Scout runtime root is not writable: {err}")
  finally:
    try:
      runtime_probe.unlink()
    except OSError:
      pass

  browser_ok = None
  if getattr(args, "check_browser", False):
    apply_url = env_default("JOB_SCOUT_WEB_APPLY_URL", DEFAULT_WEB_APPLY_URL)
    parsed = urllib.parse.urlsplit(apply_url)
    health_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/ready", "", ""))
    try:
      with urllib.request.urlopen(health_url, timeout=10) as response:
        browser_ok = response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
      browser_ok = False
    if not browser_ok:
      errors.append("website application worker health check failed")

  subscriber_count = None
  html_ready_count = None
  accessible_cv_count = None
  if getattr(args, "check_subscribers", False) and has_api_key:
    try:
      ready = cmd_list_ready(argparse.Namespace(limit=100)).get("subscribers", [])
      ready = ready if isinstance(ready, list) else []
      subscriber_count = len(ready)
      # cvFileRef is an R2 object key now; verify each CV actually exists in the
      # bucket (the GET endpoint 404s on a missing or stale ref) rather than
      # trusting a non-empty ref. Only HTML-ready subscribers are probed:
      # dispatch excludes legacy PDF-only records, so an un-migrated user must
      # not abort the install.
      html_ready = [subscriber for subscriber in ready if subscriber_cv_html_ready(subscriber)]
      html_ready_count = len(html_ready)
      accessible_cv_count = sum(
        1
        for subscriber in html_ready
        if api.cv_object_available(str(subscriber.get("userId") or subscriber.get("uid") or subscriber.get("id") or ""))
      )
      if subscriber_count == 0:
        errors.append("no Job Scout subscribers are ready")
      elif accessible_cv_count != html_ready_count:
        errors.append("one or more Job Scout subscribers have no CV in storage; re-upload is required")
    except (SystemExit, RuntimeError) as err:
      errors.append(public_text(err, 240))

  result = {
    "ok": not errors,
    "effectiveUser": effective_user(),
    "effectiveUid": os.geteuid(),
    "pendingLinksPath": str(cache_path),
    "pendingLinksParent": str(cache_parent),
    "canWritePendingLinksParent": not any("pending-link cache" in error or "temp file" in error for error in errors),
    "hasInternalApiKey": has_api_key,
    "agentBaseUrl": agent_base_url(),
    "agentBaseUrlSource": agent_base_url_source(),
    "runtimeRoot": str(RUNTIME_ROOT),
    "browserHealthy": browser_ok,
    "subscriberCount": subscriber_count,
    "htmlReadyCount": html_ready_count,
    "accessibleCvCount": accessible_cv_count,
    "errors": errors,
  }
  if errors:
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(1)
  return result


def resolve_cv_ref(user_id: str, cv_arg: str, do_upload: bool) -> str:
  """`--cv` may be a local PDF (upload it to R2) or this user's already-staged R2 key."""
  text = str(cv_arg or "").strip()
  if not text:
    raise SystemExit("--cv is required")
  expected_keys = {
    f"{user_id}/cv/cv.pdf",
    f"{user_id}/cv/pending/original.pdf",
    f"{user_id}/cv/base/cv.html",
  }
  if text in expected_keys:
    return text  # already staged via stage-cv; value is this user's R2 object key
  if Path(text).expanduser().is_file():
    return str(api.upload_cv(user_id, text).get("key") or "") if do_upload else f"{user_id}/cv/pending/original.pdf"
  # Not a real local file and not the expected staged key: never persist a bogus ref.
  raise SystemExit(f"CV file not found (and not a staged CV reference): {text}")


def cmd_stage_cv(args: argparse.Namespace) -> dict:
  return api.stage_cv(args.phone, args.cv)


def cmd_save_profile(args: argparse.Namespace) -> dict:
  phone = normalize_phone(args.phone)
  user_id = str(args.user_id or "").strip() or api.linked_user_id(phone)
  profile_confirmed = bool(getattr(args, "profile_confirmed", False))
  safety_acknowledged = bool(getattr(args, "safety_acknowledged", False))
  if not profile_confirmed:
    raise SystemExit("--profile-confirmed is required after the user confirms the profile summary")
  if not safety_acknowledged:
    raise SystemExit("--safety-acknowledged is required after the user acknowledges the scam warning")
  base = load_profile_json(args.profile_json)
  auto_apply = False if args.no_auto_apply else base.get("autoApply", True) is not False
  max_applications = (
    args.max_applications_per_run
    if args.max_applications_per_run is not None
    else base.get("maxApplicationsPerRun", 1)
  )
  resolved_locations = resolve_profile_locations(
    profile_values(base, "locations"),
    args.location,
    args.country,
    base.get("country"),
  )
  preferences = {
    **base,
    "targetRoles": clean_items([*profile_values(base, "targetRoles"), *(args.target_role or [])]),
    "locations": resolved_locations["locations"],
    "country": resolved_locations["country"],
    "language": args.language or base.get("language") or DEFAULT_JSEARCH_LANGUAGE,
    "qualifications": clean_items([*profile_values(base, "qualifications"), *(args.qualification or [])]),
    "experience": clean_items([*profile_values(base, "experience"), *(args.experience or [])]),
    "skills": clean_items([*profile_values(base, "skills"), *(args.skill or [])]),
    "exclusions": clean_items([*profile_values(base, "exclusions"), *(args.exclusion or [])]),
    "promptNotes": clean_items([*profile_values(base, "promptNotes"), *(args.prompt_note or [])]),
    "autoApply": auto_apply,
    "maxApplicationsPerRun": max_applications,
  }
  if args.dry_run:
    return {
      "dryRun": True,
      "phoneHash": phone_hash(phone),
      "userId": user_id,
      "cvFileRef": resolve_cv_ref(user_id, args.cv, do_upload=False),
      "preferences": preferences,
      "profileConfirmed": profile_confirmed,
      "safetyAcknowledged": safety_acknowledged,
      "uploadEndpoint": endpoint("/internal/job-scout/cv"),
      "endpoint": endpoint("/internal/job-scout/profile"),
    }
  cv_file_ref = resolve_cv_ref(user_id, args.cv, do_upload=True)
  payload = {
    "phone": phone,
    "userId": user_id,
    "preferences": preferences,
    "cvFileRef": cv_file_ref,
    "profileConfirmed": profile_confirmed,
    "safetyAcknowledged": safety_acknowledged,
  }
  result = api.request_json("POST", endpoint("/internal/job-scout/profile"), payload)
  remove_pending_links(phone=phone, service="job-scout")
  return result


def cmd_list_ready(args: argparse.Namespace) -> dict:
  query = urllib.parse.urlencode({"limit": args.limit})
  return api.request_json("GET", endpoint(f"/internal/job-scout/subscribers?{query}"))


# The canonical CV is stored as UNSTYLED semantic HTML (structure + data
# attributes, no <style> block) so content and presentation stay decoupled and
# the personalization agent can edit content freely. The modern stylesheet below
# is injected on the fly at render time via style_cv_html(); conversion must
# still produce a clean, semantic, standalone page — not a positional dump of
# the source PDF.
#
# Small inline SVG data-URI icons for the CONTACT lines. Data-URI backgrounds
# render reliably in headless Chromium PDF output, unlike webfont glyphs.
_CV_ICON_LOCATION = (
  "data:image/svg+xml;utf8,"
  "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%23405a78'%3E"
  "%3Cpath d='M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7zm0 9.5"
  "a2.5 2.5 0 110-5 2.5 2.5 0 010 5z'/%3E%3C/svg%3E"
)
_CV_ICON_PHONE = (
  "data:image/svg+xml;utf8,"
  "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%23405a78'%3E"
  "%3Cpath d='M6.62 10.79a15.53 15.53 0 006.59 6.59l2.2-2.2a1 1 0 011.02-.24 11.36 11.36 0 003.56.57"
  " 1 1 0 011 1V20a1 1 0 01-1 1A17 17 0 013 4a1 1 0 011-1h3.5a1 1 0 011 1 11.36 11.36 0 00.57 3.56"
  " 1 1 0 01-.24 1.02l-2.21 2.21z'/%3E%3C/svg%3E"
)
_CV_ICON_EMAIL = (
  "data:image/svg+xml;utf8,"
  "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%23405a78'%3E"
  "%3Cpath d='M20 4H4a2 2 0 00-2 2v12a2 2 0 002 2h16a2 2 0 002-2V6a2 2 0 00-2-2zm0 4l-8 5-8-5V6l8 5 8-5v2z'/%3E"
  "%3C/svg%3E"
)
CV_HTML_STYLE = """\
  @page { size: A4; margin: 0; }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: #fff; color: #202b3c; font-family: Arial, Helvetica, sans-serif; font-size: 10pt; line-height: 1.4; }
  main.cv { width: 210mm; min-height: 297mm; margin: 0 auto; padding: 12mm 14mm 11mm; }
  .cv-header { margin: 0 0 7mm; }
  h1 { margin: 0; color: #101826; font-size: 23pt; line-height: 1.08; font-weight: 700; }
  .headline { margin: 2mm 0 0; color: #405a78; font-size: 11pt; text-transform: uppercase; letter-spacing: 0.6pt; }
  .cv-grid { display: grid; grid-template-columns: 55mm minmax(0, 1fr); gap: 9mm; align-items: start; }
  .sidebar { min-width: 0; padding-right: 7mm; border-right: 1px solid #d7e0ea; }
  .main-column { min-width: 0; }
  section { break-inside: avoid; margin: 0 0 5.5mm; }
  h2 { margin: 0 0 4mm; padding: 0 0 2.5mm; color: #175ec8; font-size: 8.5pt; line-height: 1.2; font-weight: 700; text-transform: uppercase; letter-spacing: 1pt; border-bottom: 1px solid #cbdcf3; }
  .sidebar h2 { border-bottom: 0; padding-bottom: 0; }
  p { margin: 0 0 3mm; }
  .entry { margin: 0 0 3.5mm; }
  .entry-head { display: flex; justify-content: space-between; align-items: baseline; gap: 4mm; }
  .entry-meta { flex: none; color: #6b7a8d; font-size: 9pt; }
  ul { margin: 0; padding: 0; list-style: none; }
  li { position: relative; margin: 0 0 1.5mm; padding-left: 4mm; }
  li::before { content: '\\25b8'; position: absolute; left: 0; color: #175ec8; font-weight: 700; }
  .skills { display: flex; flex-wrap: wrap; gap: 2mm; }
  .skill { padding: 1.2mm 2.2mm; border: 1px solid #d7e0ea; border-radius: 2mm; font-size: 9pt; }
  .contact-line { position: relative; margin: 0 0 3mm; padding-left: 6mm; overflow-wrap: anywhere; }
  .contact-line::before { content: ''; position: absolute; left: 0; top: 0.3mm; width: 3.6mm; height: 3.6mm; background-repeat: no-repeat; background-position: center; background-size: contain; }
  .contact-line.location::before { background-image: url("%s"); }
  .contact-line.phone::before { background-image: url("%s"); }
  .contact-line.email::before { background-image: url("%s"); }
  .language { display: flex; justify-content: space-between; gap: 3mm; margin: 0 0 1.5mm; }
  .language .level { flex: none; color: #6b7a8d; }
  .sidebar p, .sidebar li { overflow-wrap: anywhere; }
  @media print { main.cv { margin: 0; } }
""" % (_CV_ICON_LOCATION, _CV_ICON_PHONE, _CV_ICON_EMAIL)


SIDE_SECTION_TITLES = {"CONTACT", "SKILLS", "LANGUAGES", "REFERENCES"}
IMMUTABLE_SECTION_TITLES = {"CONTACT", "EDUCATION", "CERTIFICATIONS", "REFERENCES"}
KNOWN_SECTION_TITLES = SIDE_SECTION_TITLES | IMMUTABLE_SECTION_TITLES | {
  "ACHIEVEMENTS",
  "ADDITIONAL INFORMATION",
  "EMPLOYMENT",
  "EXPERIENCE",
  "INTERESTS",
  "PROFESSIONAL EXPERIENCE",
  "PROFESSIONAL PROFILE",
  "PROFESSIONAL SUMMARY",
  "PROFILE",
  "PROJECTS",
  "SUMMARY",
  "TRAINING",
  "WORK EXPERIENCE",
}
_BROWSER_TIMESTAMP = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4},?\s+\d{1,2}:\d{2}\s+(?:AM|PM)$", re.IGNORECASE)
_PAGE_COUNTER = re.compile(r"^\d+\s*/\s*\d+$")


def _clean_cv_line(value: str) -> str:
  line = " ".join(str(value or "").replace("\ufffd", "").split())
  if not line or line.casefold().startswith(("file://", "about:blank")):
    return ""
  if _BROWSER_TIMESTAMP.fullmatch(line) or _PAGE_COUNTER.fullmatch(line):
    return ""
  phone = re.search(r"\+\d[\d\s()\-]{6,}", line)
  if phone:
    return phone.group(0).strip()
  email = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]*", line)
  if email:
    return email.group(0).strip()
  line = re.sub(r"^[\u00a2\u00a9@]+\s*", "", line)
  return line


def _sanitized_cv_text(text: str) -> str:
  lines = [_clean_cv_line(line) for line in text.replace("\f", "\n").splitlines()]
  return "\n".join(lines).strip()


def _cv_text_blocks(text: str) -> list[list[str]]:
  """Blank-line-separated blocks of whitespace-normalized text lines."""
  blocks: list[list[str]] = []
  current: list[str] = []
  for raw_line in _sanitized_cv_text(text).splitlines():
    line = _clean_cv_line(raw_line)
    if line and line.rstrip(":").upper() in KNOWN_SECTION_TITLES:
      if current:
        blocks.append(current)
        current = []
      blocks.append([line])
      continue
    if line:
      current.append(line)
    elif current:
      blocks.append(current)
      current = []
  if current:
    blocks.append(current)
  return blocks


def _looks_like_heading(block: list[str]) -> bool:
  if len(block) != 1:
    return False
  line = block[0]
  if len(line) > 60:
    return False
  letters = [character for character in line if character.isalpha()]
  mostly_upper = bool(letters) and sum(character.isupper() for character in letters) / len(letters) > 0.8
  return mostly_upper or line.endswith(":")


def _section_title(block: list[str]) -> str:
  return block[0].rstrip(":").upper()


_BULLET_MARKER = re.compile(r"^[>\u00bb\u203a\u25b8\u2039\u2022\u2023\u2043\-]+\s*")


def _is_lone_marker(line: str) -> bool:
  """True when a line is nothing but a bullet marker (no following text)."""
  return bool(line) and not _BULLET_MARKER.sub("", line).strip()


def _join_orphaned_markers(block: list[str]) -> list[str]:
  """Fold a lone-marker line onto the following text line so no orphan survives."""
  joined: list[str] = []
  pending_marker = False
  for line in block:
    if _is_lone_marker(line):
      pending_marker = True
      continue
    if pending_marker and line:
      # Re-attach the dropped marker so the block still reads as a bullet list.
      line = f"\u25b8 {_BULLET_MARKER.sub('', line).strip()}"
    pending_marker = False
    if line:
      joined.append(line)
  return joined


def _render_cv_block(block: list[str]) -> str:
  block = _join_orphaned_markers(block)
  if not block:
    return ""
  cleaned = [_BULLET_MARKER.sub("", line).strip() for line in block]
  if all(re.match(r"^[>\u00bb\u203a\u25b8\u2039\u2022\u2023\u2043\-]", line) for line in block):
    items = "\n".join(f"          <li>{html_module.escape(item)}</li>" for item in cleaned if item)
    return f"        <ul>\n{items}\n        </ul>" if items else ""
  body = "<br>\n          ".join(html_module.escape(line) for line in cleaned if line)
  return f"        <p class=\"entry\">{body}</p>" if body else ""


def _repair_contact_blocks(blocks: list[list[str]]) -> list[list[str]]:
  repaired: list[list[str]] = []
  for block in blocks:
    lines: list[str] = []
    for line in block:
      if lines and "@" in lines[-1] and "@" not in line and re.fullmatch(r"[A-Za-z]{1,4}\.com", line):
        lines[-1] += line
      else:
        lines.append(line)
    lines = [re.sub(r"@gmai(?:i|l)+\.com$", "@gmail.com", line, flags=re.IGNORECASE) for line in lines]
    repaired.append(lines)
  return repaired


def _render_contact_line(line: str) -> str:
  """Tag a CONTACT line with location/phone/email so the stylesheet can add an icon."""
  text = _BULLET_MARKER.sub("", line).strip()
  if not text:
    return ""
  if "@" in text:
    kind = "email"
  elif re.search(r"\+?\d[\d\s()\-]{5,}", text):
    kind = "phone"
  else:
    kind = "location"
  return f'        <p class="contact-line {kind}">{html_module.escape(text)}</p>'


# A recognized fluency level trailing a LANGUAGES line, right-aligned in render.
_LANGUAGE_LEVEL = re.compile(
  r"[\s\-–—:]+(native|fluent|proficient|advanced|intermediate|conversational|basic|beginner|elementary|mother tongue)\.?$",
  re.IGNORECASE,
)


def _render_language_line(line: str) -> str:
  """Split a LANGUAGES entry into the name plus an optionally right-aligned level."""
  text = _BULLET_MARKER.sub("", line).strip()
  if not text:
    return ""
  match = _LANGUAGE_LEVEL.search(text)
  if not match:
    return f'        <p class="language"><span class="name">{html_module.escape(text)}</span></p>'
  name = text[: match.start()].strip(" \t-–—:") or text
  level = match.group(1)
  return (
    '        <p class="language">'
    f'<span class="name">{html_module.escape(name)}</span>'
    f'<span class="level">{html_module.escape(level.title())}</span>'
    "</p>"
  )


def _render_cv_section(title: str, blocks: list[list[str]], sidebar: bool) -> str:
  title_attr = html_module.escape(title.title(), quote=True)
  immutable = f' data-cv-immutable="section-{title_attr}"' if title in IMMUTABLE_SECTION_TITLES else ""
  lines: list[str] = []
  if title == "CONTACT":
    blocks = _repair_contact_blocks(blocks)
  if title == "SKILLS":
    skills = [html_module.escape(line) for block in blocks for line in block if line]
    content = "\n".join(f'          <span class="skill">{skill}</span>' for skill in skills)
    lines.append(f'        <div class="skills">\n{content}\n        </div>')
  elif title == "CONTACT":
    for line in (line for block in blocks for line in block if line):
      lines.append(_render_contact_line(line))
  elif title == "LANGUAGES":
    for line in (line for block in blocks for line in block if line):
      rendered = _render_language_line(line)
      if rendered:
        lines.append(rendered)
  elif title in {"PROFESSIONAL PROFILE", "PROFILE", "SUMMARY", "PROFESSIONAL SUMMARY"}:
    paragraph = " ".join(line for block in blocks for line in block)
    lines.append(f"        <p>{html_module.escape(paragraph)}</p>")
  else:
    for block in blocks:
      rendered = _render_cv_block(block)
      if rendered:
        lines.append(rendered)
  if not lines:
    return ""
  section_class = " side-section" if sidebar else ""
  return (
    f'      <section class="cv-section{section_class}" data-cv-section="{title_attr}"{immutable}>\n'
    f"        <h2>{html_module.escape(title.title())}</h2>\n"
    + "\n".join(lines)
    + "\n      </section>"
  )


def cv_html_document(text: str, styled: bool = False) -> str:
  blocks = _cv_text_blocks(text)
  if not blocks:
    raise RuntimeError("no text could be extracted from the CV PDF")
  title = blocks[0][0]
  introductory = blocks[0][1:]
  remaining = blocks[1:]
  headline = ""
  if remaining and len(remaining[0]) == 1 and len(remaining[0][0]) <= 80:
    possible_headline = _section_title(remaining[0])
    if possible_headline not in KNOWN_SECTION_TITLES:
      headline = remaining.pop(0)[0]

  sections: list[tuple[str, list[list[str]]]] = []
  current_title = "CONTACT" if introductory else ""
  current_blocks: list[list[str]] = [introductory] if introductory else []
  for block in remaining:
    if _looks_like_heading(block):
      if current_title and current_blocks:
        sections.append((current_title, current_blocks))
      current_title = _section_title(block)
      current_blocks = []
    else:
      if not current_title:
        current_title = "PROFESSIONAL PROFILE"
      current_blocks.append(block)
  if current_title and current_blocks:
    sections.append((current_title, current_blocks))

  sidebar_sections = [_render_cv_section(name, values, True) for name, values in sections if name in SIDE_SECTION_TITLES]
  main_sections = [_render_cv_section(name, values, False) for name, values in sections if name not in SIDE_SECTION_TITLES]
  if not main_sections and sidebar_sections:
    main_sections, sidebar_sections = sidebar_sections, []
  headline_html = f'\n      <p class="headline">{html_module.escape(headline)}</p>' if headline else ""
  sidebar_html = "\n".join(section for section in sidebar_sections if section)
  main_html = "\n".join(section for section in main_sections if section)
  # The base CV is stored UNSTYLED (no <style> block); the modern stylesheet is
  # injected at render time by style_cv_html(). A styled document is still
  # available via styled=True for any caller that wants a standalone page.
  style_html = f"<style>\n{CV_HTML_STYLE}</style>\n" if styled else ""
  return (
    "<!doctype html>\n"
    "<html lang=\"en\">\n"
    "<head>\n"
    "<meta charset=\"utf-8\">\n"
    f"<title>{html_module.escape(title)} - CV</title>\n"
    f"{style_html}"
    "</head>\n"
    "<body>\n"
    "  <main class=\"cv\" data-cv-template=\"professional-v2\">\n"
    "    <header class=\"cv-header\">\n"
    f"      <h1 data-cv-field=\"identity\">{html_module.escape(title)}</h1>{headline_html}\n"
    "    </header>\n"
    "    <div class=\"cv-grid\">\n"
    f"      <aside class=\"sidebar\">\n{sidebar_html}\n      </aside>\n"
    f"      <div class=\"main-column\">\n{main_html}\n      </div>\n"
    "    </div>\n"
    "  </main>\n"
    "</body>\n"
    "</html>\n"
  )


def style_cv_html(html: str) -> str:
  """Inject the modern stylesheet into a semantic (unstyled) CV document.

  Idempotent-safe: if the document already carries a <style> block (a legacy or
  already-styled document), it is returned unchanged rather than double-styled.
  """
  text = str(html or "")
  if re.search(r"<style\b", text, flags=re.IGNORECASE):
    return text
  style_block = f"<style>\n{CV_HTML_STYLE}</style>\n"
  head_close = re.search(r"</head\s*>", text, flags=re.IGNORECASE)
  if head_close:
    return text[: head_close.start()] + style_block + text[head_close.start() :]
  head_open = re.search(r"<head\b[^>]*>", text, flags=re.IGNORECASE)
  if head_open:
    return text[: head_open.end()] + "\n" + style_block + text[head_open.end() :]
  # No <head> at all: wrap the style before the first content so it still applies.
  return style_block + text


def _ocr_cv_text(pdf_path: Path) -> str:
  with tempfile.TemporaryDirectory() as tmp:
    prefix = Path(tmp) / "page"
    subprocess.run(
      ["pdftoppm", "-png", "-r", "180", str(pdf_path), str(prefix)],
      check=True,
      capture_output=True,
      timeout=180,
    )
    def page_number(path: Path) -> int:
      match = re.search(r"(\d+)$", path.stem)
      return int(match.group(1)) if match else 0

    pages = sorted(Path(tmp).glob("page-*.png"), key=page_number) or sorted(Path(tmp).glob("page*.png"), key=page_number)
    if not pages:
      raise RuntimeError("CV OCR produced no pages")
    extracted: list[str] = []
    for page in pages:
      completed = subprocess.run(
        ["tesseract", str(page), "stdout"],
        check=True,
        capture_output=True,
        timeout=120,
      )
      extracted.append(completed.stdout.decode("utf-8", errors="replace"))
    return "\n\n".join(extracted)


def _pdftotext_cv_text(pdf_path: Path, layout: bool = False) -> str:
  command = ["pdftotext", "-enc", "UTF-8"]
  if layout:
    # -layout keeps text at its physical x-position so side-by-side columns stay
    # spatially separated instead of being read across the page row by row.
    command.append("-layout")
  command += [str(pdf_path), "-"]
  completed = subprocess.run(command, check=True, capture_output=True, timeout=120)
  return completed.stdout.decode("utf-8", errors="replace")


def _decolumn_layout_text(text: str) -> str | None:
  """De-interleave a two-column CV from layout-preserving pdftotext output.

  A two-column CV (a narrow sidebar beside a wide main column) is read across
  the page by plain extraction, interleaving unrelated lines (a contact address
  landing inside the profile paragraph). Layout-preserving output keeps each
  column at its physical x, so a vertical band of whitespace separates them. We
  detect that gutter and emit the left column top-to-bottom, then the right,
  restoring per-column reading order. Returns None when no confident two-column
  structure is found so single-column CVs fall through unchanged.
  """
  lines = [line.rstrip() for line in text.replace("\f", "\n").split("\n")]
  content = [line for line in lines if line.strip()]
  if len(content) < 8:
    return None
  width = max(len(line) for line in content)
  if width < 40:
    return None
  total = len(content)

  def space_fraction(column: int) -> float:
    return sum(1 for line in content if column >= len(line) or line[column] == " ") / total

  low, high = int(width * 0.15), int(width * 0.75)
  gutter_columns = [column for column in range(low, high + 1) if space_fraction(column) >= 0.95]
  if not gutter_columns:
    return None
  # Collapse the always-space columns into contiguous bands and take the widest;
  # a real gutter is several characters wide, not an incidental aligned space.
  bands: list[tuple[int, int]] = []
  start = previous = gutter_columns[0]
  for column in gutter_columns[1:]:
    if column == previous + 1:
      previous = column
    else:
      bands.append((start, previous))
      start = previous = column
  bands.append((start, previous))
  bands = [band for band in bands if band[1] - band[0] + 1 >= 2]
  if not bands:
    return None
  band = max(bands, key=lambda item: item[1] - item[0])
  split = (band[0] + band[1]) // 2

  left: list[str] = []
  right: list[str] = []

  def append(column: list[str], value: str) -> None:
    if value:
      column.append(value)
    elif column and column[-1] != "":
      column.append("")

  for line in lines:
    if not line.strip():
      append(left, "")
      append(right, "")
      continue
    append(left, line[:split].rstrip())
    append(right, line[split:].strip())

  left_rows = sum(1 for value in left if value)
  right_rows = sum(1 for value in right if value)
  # Both sides must be substantially populated; otherwise this is a
  # single-column document that merely has ragged right-hand whitespace.
  if left_rows < total * 0.25 or right_rows < total * 0.25:
    return None
  return "\n".join(left).strip() + "\n\n" + "\n".join(right).strip()


def _extract_cv_text(pdf_path: Path) -> str:
  # Prefer layout-preserving extraction so a two-column CV is de-interleaved
  # into per-column reading order before parsing; only genuinely two-column
  # documents take this path, everything else falls through unchanged.
  try:
    decolumned = _decolumn_layout_text(_pdftotext_cv_text(pdf_path, layout=True))
    if decolumned:
      return decolumned
  except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
    pass
  try:
    return _ocr_cv_text(pdf_path)
  except FileNotFoundError as err:
    try:
      return _pdftotext_cv_text(pdf_path)
    except FileNotFoundError:
      raise RuntimeError("tesseract or pdftotext is required to convert pending CVs") from err
  except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
    try:
      return _pdftotext_cv_text(pdf_path)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
      raise RuntimeError("could not extract readable text from the pending CV") from err


def convert_pdf_to_cv_html(pdf_path: Path, html_path: Path) -> Path:
  html_path.write_text(cv_html_document(_extract_cv_text(pdf_path)), encoding="utf-8")
  return html_path


def pending_conversion_subscribers(limit: int) -> list[dict]:
  query = urllib.parse.urlencode({"limit": limit, "status": "pending_conversion"})
  result = api.request_json("GET", endpoint(f"/internal/job-scout/subscribers?{query}"))
  subscribers = result.get("subscribers") if isinstance(result, dict) else []
  if not isinstance(subscribers, list):
    return []
  # A backend without the status filter ignores it and returns ready
  # subscribers; those carry no pending CV, so this guard keeps the pass a
  # no-op there instead of re-converting finished CVs.
  return [
    subscriber for subscriber in subscribers
    if str(subscriber.get("cvConversionStatus") or "").strip().casefold() == "pending"
  ]


def convert_pending_cvs_best_effort(limit: int = 100) -> dict:
  """Pre-dispatch conversion pass; conversion problems never block dispatch."""
  try:
    return cmd_convert_pending_cvs(argparse.Namespace(limit=limit, user_id=None, dry_run=False))
  except (RuntimeError, SystemExit, OSError, ValueError) as err:
    return {"ok": False, "error": str(err) or "conversion pass failed"}


def cmd_convert_pending_cvs(args: argparse.Namespace) -> dict:
  subscribers = pending_conversion_subscribers(args.limit)
  wanted = {str(value).strip() for value in (args.user_id or []) if str(value).strip()}
  if wanted:
    subscribers = [subscriber for subscriber in subscribers if subscriber_user_id(subscriber) in wanted]
  results: list[dict] = []
  converted_count = 0
  failed_count = 0
  for subscriber in subscribers:
    user_id = subscriber_user_id(subscriber)
    entry: dict = {
      "userKey": user_key(user_id),
      "whatsappPhone": subscriber.get("whatsappPhone"),
    }
    if args.dry_run:
      entry["status"] = "dry_run"
      results.append(entry)
      continue
    try:
      with tempfile.TemporaryDirectory(prefix="job-scout-cv-convert-") as tmp:
        pdf_path = api.download_cv(user_id, Path(tmp) / "cv.pdf", format="pending")
        html_path = convert_pdf_to_cv_html(pdf_path, Path(tmp) / "cv.html")
        upload = api.upload_cv_html(user_id, html_path)
      entry["status"] = "converted"
      entry["ready"] = bool(upload.get("ready"))
      converted_count += 1
    except (RuntimeError, SystemExit, OSError) as err:
      # One bad CV must not block the rest of the pending queue.
      entry["status"] = "failed"
      entry["error"] = str(err) or "conversion failed"
      failed_count += 1
    results.append(entry)
  return {
    "ok": failed_count == 0,
    "pendingCount": len(subscribers),
    "convertedCount": converted_count,
    "failedCount": failed_count,
    "results": results,
  }


def cmd_status(args: argparse.Namespace) -> dict:
  """Return requester-scoped readiness from the authoritative backend evaluator."""
  phone = normalize_phone(args.phone)
  query = urllib.parse.urlencode({"phone": phone})
  return api.request_json("GET", endpoint(f"/internal/job-scout/status?{query}"))


def cmd_list_applications(args: argparse.Namespace) -> dict:
  query = urllib.parse.urlencode({"userId": args.user_id})
  return api.request_json("GET", endpoint(f"/internal/job-scout/applications?{query}"))


def cmd_record_application(args: argparse.Namespace) -> dict:
  payload = {
    "userId": args.user_id,
    "company": args.company,
    "role": args.role,
    "status": args.status,
    "applicationEmail": args.application_email,
    "source": args.source,
    "sourceUrl": args.source_url,
    "notes": args.notes,
    "messageId": args.message_id,
    "closing": args.closing,
    "matchReason": args.match_reason,
    "submissionMethod": getattr(args, "submission_method", None),
    "applicationUrl": getattr(args, "application_url", None),
    "blockerCode": getattr(args, "blocker_code", None),
    "attemptCount": getattr(args, "attempt_count", None),
    "evidence": getattr(args, "evidence", None),
    "replace": args.replace,
  }
  return api.request_json("POST", endpoint("/internal/job-scout/applications"), payload)


def cmd_fetch_candidates(args: argparse.Namespace) -> dict:
  base = load_profile_json(args.profile_json)
  resolved_locations = resolve_profile_locations(
    profile_values(base, "locations"),
    args.location,
    args.country,
    base.get("country"),
  )
  preferences = {
    **base,
    "targetRoles": clean_items([*profile_values(base, "targetRoles"), *(args.target_role or [])]),
    "locations": resolved_locations["locations"],
    "country": resolved_locations["country"],
    "language": args.language or base.get("language") or DEFAULT_JSEARCH_LANGUAGE,
    "exclusions": clean_items([*profile_values(base, "exclusions"), *(args.exclusion or [])]),
    "maxApplicationsPerRun": args.limit,
  }
  raw_candidates = (
    fetch_zimlinks_candidates(preferences, args.limit)
    + fetch_jsearch_candidates(preferences, args.limit)
    + fetch_scraped_candidates(preferences, args.limit)
  )
  candidates = filter_candidates(raw_candidates, preferences, [], args.limit)
  queries = search_queries(preferences)
  return {
    "ok": True,
    "provider": jsearch_provider() if queries else None,
    "sources": sorted({str(candidate.get("source") or "jsearch") for candidate in candidates}),
    "queries": queries,
    "country": preference_code(preferences, "country", DEFAULT_JSEARCH_COUNTRY),
    "language": preference_code(preferences, "language", DEFAULT_JSEARCH_LANGUAGE),
    "candidateCount": len(candidates),
    "candidates": candidates,
  }


def cmd_resolve_location(args: argparse.Namespace) -> dict:
  resolved = resolve_preferred_locations(args.location)
  return {"ok": True, **resolved}
