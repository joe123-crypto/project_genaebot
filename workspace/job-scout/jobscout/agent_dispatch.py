"""OpenClaw-agent orchestration for Job Scout.

The language model is deliberately outside this module.  These commands make
deterministic work packets for isolated OpenClaw children, validate their
structured decisions and files, and retain exclusive ownership of application
side effects.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import html as html_module
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import secrets
from typing import Iterable

from . import api, application, dispatch_store
from .application import (
  action_instruction,
  applications_from_response,
  build_user_summary,
  candidate_application_url,
  candidate_attempt,
  cover_letter_text,
  match_reason,
  web_apply_payload,
  write_cover_letter,
)
from .commands_dispatch import (
  cmd_finish_dispatch,
  cmd_record_dispatch_outcome,
  cmd_start_dispatch,
  cmd_verify_dispatch,
)
from .commands_group_feed import maybe_write_group_feed_cache
from .commands_setup import cv_html_document, cmd_list_applications, cmd_list_ready, convert_pdf_to_cv_html, convert_pending_cvs_best_effort
from .constants import UNKNOWN_EMPLOYER, UNKNOWN_ROLE, WHATSAPP_CHANNEL_SOURCES
from .formality import employer_formality
from .delivery import (
  MAX_RENDER_HTML_BYTES,
  gmail_sender_connected,
  request_web_application,
  result_message_id,
  send_application_email,
  send_whatsapp_summary,
)
from .dispatch_runner import (
  browser_apply_enabled,
  publish_user_dashboard_telemetry,
  record_candidate_application,
  scoped_subscribers,
  subscriber_is_dispatch_ready,
)
from .jsearch import (
  apply_source_filter_stats,
  candidate_key,
  duplicate_key,
  fetch_candidate_sources,
  filter_candidates_for_user_with_stats,
  finalize_channel_candidate_dispatch,
  global_filter_candidates,
  record_channel_candidate_outcome,
  register_channel_candidate_eligibility,
)
from .profile import (
  max_applications,
  search_queries,
  subscriber_preferences,
  subscriber_user_id,
  user_key,
)
from .subscriber import subscriber_cv_html, subscriber_phone
from .textutil import public_text
from .util import safe_run_id, utc_now


AGENT_RESULT_SCHEMA_VERSION = 1
AGENT_MATCH_THRESHOLD = 70
AGENT_SHORTLIST_LIMIT = 20
AGENT_COMPLETION_STALE_SECONDS = 45 * 60
# Matches the render worker's request cap so a validated CV cannot fail at
# delivery time for being too large.
MAX_HTML_BYTES = MAX_RENDER_HTML_BYTES
MAX_LETTER_BYTES = 128 * 1024
# A tailored letter that clears validation must be at least this substantial; an
# empty file or an unfilled one-line placeholder is rejected in favour of the
# deterministic template fallback.
MIN_LETTER_CHARS = 200
MIN_LETTER_LINES = 4
# The agent's only in-repo contract for the letter: a small, declarative
# instruction it applies to the per-candidate baseCoverLetter draft.
COVER_LETTER_GUIDANCE = (
  "Rewrite baseCoverLetter into a letter tailored to this specific job. Name the "
  "exact role and employer, and address the job's stated requirements/description "
  "using only facts already present in the candidate's CV — never invent "
  "qualifications or experience. Keep it plain text (no HTML or markdown), around "
  "4 to 6 short paragraphs, professional and truthful. End with a signature line. "
  "Match the register to each candidate's employerFormality: when it is \"formal\" "
  "(government or public-sector employer), write more formally — avoid "
  "contractions, use a measured, respectful tone, open with \"Dear Sir/Madam,\" and "
  "close with \"Yours faithfully,\"; when it is \"standard\", keep the ordinary "
  "professional tone. The baseCoverLetter draft already reflects the correct "
  "register, so preserve it while tailoring."
)
TERMINAL_CLAIM_STATUSES = {"completed", "fallback_completed"}
# html.parser never emits an endtag for a bare void element, so these tags
# must not affect immutable-region depth in either the <br> or <br/> form.
VOID_ELEMENTS = frozenset({
  "area", "base", "br", "col", "embed", "hr", "img", "input",
  "link", "meta", "param", "source", "track", "wbr",
})


class AgentResultError(ValueError):
  """The child response is unsafe or inconsistent with its claimed packet."""


def _source_results() -> dict[str, dict]:
  return {
    source: {"status": "skipped", "rawCount": 0, "durationMs": 0}
    for source in (*WHATSAPP_CHANNEL_SOURCES, "jsearch", "vacancymail", "iharare", "vacancybox", "applynow")
  }


def _agent_candidate(candidate: dict) -> dict:
  output = dict(candidate)
  output["agentCandidateId"] = candidate_key(candidate)
  # Surface the register per candidate so the agent's tailored letter/profile
  # wording matches the employer's formality (government/public-sector jobs read
  # more formally). Same classifier the deterministic fallback letter uses.
  output["employerFormality"] = employer_formality(candidate)
  return output


def _claim_summary(claim: dict) -> dict:
  return {
    "claimId": claim["claimId"],
    "userKey": claim["userKey"],
    "status": claim["status"],
    "candidateCount": int(claim.get("candidateCount") or 0),
  }


def _load_claims(root: Path, run_id: str) -> list[dict]:
  marker = dispatch_store.load_dispatch(run_id, root)
  claim_ids = marker.get("claimIds") if isinstance(marker.get("claimIds"), list) else []
  return [dispatch_store.load_agent_claim(root, run_id, str(claim_id)) for claim_id in claim_ids]


def _resume_result(root: Path, run_id: str) -> dict:
  marker = dispatch_store.load_dispatch(run_id, root)
  claims = _load_claims(root, run_id)
  return {
    "ok": True,
    "agentPhase": "prepare",
    "resumed": True,
    "runId": run_id,
    "readyCount": int(marker.get("readyCount") or len(claims)),
    "claimCount": len(claims),
    "terminalCount": sum(claim.get("status") in TERMINAL_CLAIM_STATUSES for claim in claims),
    "claims": [_claim_summary(claim) for claim in claims],
  }


def _reserve_agent_run(args: argparse.Namespace) -> tuple[str, bool]:
  requested = safe_run_id(args.run_id) if args.run_id else None
  with dispatch_store.agent_state_lock(args.dispatch_root):
    active_run = dispatch_store.resolve_active_agent_run(args.dispatch_root)
    if active_run:
      return active_run, True

    run_id = requested or f"job-scout-agent-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    run_id = safe_run_id(run_id)
    cmd_start_dispatch(argparse.Namespace(run_id=run_id, ready_count=0, dispatch_root=args.dispatch_root))
    marker = dispatch_store.load_dispatch(run_id, args.dispatch_root)
    marker.update({
      "schemaVersion": 3,
      "mode": "openclaw-agent",
      "phase": "preparing",
      "claimIds": [],
      "updatedAt": utc_now(),
    })
    dispatch_store.save_dispatch(marker, args.dispatch_root)
    dispatch_store.set_active_agent_dispatch(args.dispatch_root, {
      "runId": run_id,
      "startedAt": marker["startedAt"],
      "mode": "openclaw-agent",
    })
    return run_id, False


def cmd_agent_prepare(args: argparse.Namespace) -> dict:
  """Persist one deterministic shortlist/claim for every ready subscriber."""
  # The long lock excludes the legacy one-process dispatcher while discovery is
  # building the durable run. Child completion intentionally does not hold it.
  lock_run = args.run_id or "job-scout-agent-prepare"
  with dispatch_store.exclusive_dispatch_lock(args.dispatch_root, safe_run_id(lock_run)):
    run_id, resumed = _reserve_agent_run(args)
    marker = dispatch_store.load_dispatch(run_id, args.dispatch_root)
    if resumed and marker.get("phase") in {"ready", "running", "finishing"}:
      return _resume_result(args.dispatch_root, run_id)

    # Convert any pending PDF CVs first so those subscribers are ready for
    # this very run instead of waiting a day; failures never block dispatch.
    cv_conversion = convert_pending_cvs_best_effort(args.limit_ready)

    ready_result = cmd_list_ready(argparse.Namespace(limit=args.limit_ready))
    subscribers = ready_result.get("subscribers") if isinstance(ready_result, dict) else []
    subscribers = subscribers if isinstance(subscribers, list) else []
    subscribers = [subscriber for subscriber in subscribers if subscriber_is_dispatch_ready(subscriber)]
    subscribers = scoped_subscribers(subscribers, getattr(args, "phone", None))
    shortlist_limit = max(1, min(int(getattr(args, "shortlist_limit", AGENT_SHORTLIST_LIMIT)), AGENT_SHORTLIST_LIMIT))
    maybe_write_group_feed_cache(args, run_id)

    prepared: list[dict] = []
    raw_pool: list[dict] = []
    for subscriber in subscribers:
      user_id = subscriber_user_id(subscriber)
      preferences = subscriber_preferences(subscriber)
      source_results = _source_results()
      try:
        applications = applications_from_response(cmd_list_applications(argparse.Namespace(user_id=user_id)))
        raw_candidates: list[dict] = []
        if search_queries(preferences):
          raw_candidates, source_results = fetch_candidate_sources(preferences, shortlist_limit)
        raw_pool.extend(raw_candidates)
        prepared.append({
          "subscriber": subscriber,
          "userId": user_id,
          "preferences": preferences,
          "applications": applications,
          "rawKeys": [duplicate_key(candidate) for candidate in raw_candidates],
          "sourceResults": source_results,
          "preparationError": None,
        })
      except SystemExit:
        raise
      except Exception as err:
        prepared.append({
          "subscriber": subscriber,
          "userId": user_id,
          "preferences": preferences,
          "applications": [],
          "rawKeys": [],
          "sourceResults": source_results,
          "preparationError": public_text(err),
        })

    _cleaned, global_stats, global_candidates = global_filter_candidates(raw_pool)
    claim_ids: list[str] = []
    claim_summaries: list[dict] = []
    for item in prepared:
      seen: set[tuple[str, str]] = set()
      pool: list[dict] = []
      for key in item["rawKeys"]:
        if key in seen:
          continue
        seen.add(key)
        candidate = global_candidates.get(key)
        if candidate is not None:
          pool.append(candidate)
      candidates, filter_stats = filter_candidates_for_user_with_stats(
        pool,
        item["preferences"],
        item["applications"],
        shortlist_limit,
      )
      apply_source_filter_stats(item["sourceResults"], filter_stats)
      candidates = [_agent_candidate(candidate) for candidate in candidates]
      user_id = item["userId"]
      if candidates:
        register_channel_candidate_eligibility(run_id, user_id, candidates)
      claim_id = secrets.token_urlsafe(18)
      claim = {
        "schemaVersion": 1,
        "runId": run_id,
        "claimId": claim_id,
        "userKey": user_key(user_id),
        "status": "pending",
        "candidateCount": len(candidates),
        "createdAt": utc_now(),
        "claimedAt": None,
        "completedAt": None,
      }
      work = {
        "schemaVersion": 1,
        "runId": run_id,
        "claimId": claim_id,
        "subscriber": item["subscriber"],
        "userId": user_id,
        "preferences": item["preferences"],
        "applications": item["applications"],
        "applicationLimit": max(0, max_applications(item["preferences"])),
        "matchThreshold": int(getattr(args, "match_threshold", AGENT_MATCH_THRESHOLD)),
        "candidates": candidates,
        "sourceResults": item["sourceResults"],
        "globalFilter": global_stats,
        "preparationError": item["preparationError"],
      }
      dispatch_store.save_agent_work(args.dispatch_root, run_id, claim_id, work)
      dispatch_store.save_agent_claim(args.dispatch_root, run_id, claim_id, claim)
      claim_ids.append(claim_id)
      claim_summaries.append(_claim_summary(claim))

    with dispatch_store.agent_state_lock(args.dispatch_root):
      marker = dispatch_store.load_dispatch(run_id, args.dispatch_root)
      marker.update({
        "phase": "ready",
        "readyCount": len(subscribers),
        "claimIds": claim_ids,
        "updatedAt": utc_now(),
      })
      dispatch_store.save_dispatch(marker, args.dispatch_root)

  return {
    "ok": True,
    "agentPhase": "prepare",
    "resumed": False,
    "runId": run_id,
    "readyCount": len(subscribers),
    "claimCount": len(claim_ids),
    "terminalCount": 0,
    "claims": claim_summaries,
    "cvConversion": cv_conversion,
  }


def cmd_agent_claim(args: argparse.Namespace) -> dict:
  run_id = safe_run_id(args.run_id)
  with dispatch_store.agent_state_lock(args.dispatch_root):
    claim = dispatch_store.load_agent_claim(args.dispatch_root, run_id, args.claim_id)
    if claim["status"] in TERMINAL_CLAIM_STATUSES:
      raise SystemExit("Job Scout claim is already complete")
    if claim["status"] == "completing":
      raise SystemExit("Job Scout claim is already being completed")
    if claim["status"] not in {"pending", "claimed"}:
      raise SystemExit("Job Scout claim is not available")
    if claim["status"] == "pending":
      claim["status"] = "claimed"
      claim["claimedAt"] = utc_now()
      dispatch_store.save_agent_claim(args.dispatch_root, run_id, args.claim_id, claim)

  work = dispatch_store.load_agent_work(args.dispatch_root, run_id, args.claim_id)
  output_dir = dispatch_store.agent_claim_output_dir(args.dispatch_root, run_id, args.claim_id)
  personalized_dir = output_dir / "personalized"
  personalized_dir.mkdir(parents=True, exist_ok=True)
  os.chmod(output_dir, 0o700)
  os.chmod(personalized_dir, 0o700)
  base_cv = output_dir / "base-cv.html"
  if not base_cv.is_file():
    canonical_path = Path(subscriber_cv_html(work["subscriber"]))
    canonical_html = canonical_path.read_text(encoding="utf-8")
    # Re-convert stale/dirty canonical HTML from the original PDF: the old
    # Georgia single-column template, anything missing the current template
    # marker, or anything still carrying browser print chrome (file:// paths or
    # a browser timestamp) must be rebuilt into clean semantic HTML.
    has_browser_chrome = bool(
      re.search(r"file://", canonical_html, flags=re.IGNORECASE)
      or re.search(
        r"\b\d{1,2}/\d{1,2}/\d{2,4},?\s+\d{1,2}:\d{2}\s+(?:AM|PM)\b",
        canonical_html,
        flags=re.IGNORECASE,
      )
    )
    legacy_generated = (
      "font-family: Georgia, 'Times New Roman', serif" in canonical_html
      or 'data-cv-template="professional-v2"' not in canonical_html
      or has_browser_chrome
    )
    if legacy_generated:
      source_pdf = output_dir / "base-source.pdf"
      try:
        api.download_cv(work["userId"], source_pdf, format="pending")
        convert_pdf_to_cv_html(source_pdf, base_cv)
      except (RuntimeError, SystemExit, OSError, ValueError):
        sanitized = re.sub(r"file://[^<\s]+", "", canonical_html, flags=re.IGNORECASE)
        sanitized = re.sub(
          r"\b\d{1,2}/\d{1,2}/\d{2,4},?\s+\d{1,2}:\d{2}\s+(?:AM|PM)\b",
          "",
          sanitized,
          flags=re.IGNORECASE,
        )
        body_match = re.search(r"<body\b[^>]*>(.*?)</body>", sanitized, flags=re.IGNORECASE | re.DOTALL)
        body = body_match.group(1) if body_match else sanitized
        body = re.sub(r"<(?:br|/p|/h[1-6]|/li)\b[^>]*>", "\n\n", body, flags=re.IGNORECASE)
        plain_text = html_module.unescape(re.sub(r"<[^>]+>", "", body))
        try:
          base_cv.write_text(cv_html_document(plain_text), encoding="utf-8")
        except RuntimeError:
          base_cv.write_text(sanitized, encoding="utf-8")
      finally:
        try:
          source_pdf.unlink()
        except OSError:
          pass
    else:
      # The canonical HTML carries the current template marker and no browser
      # chrome; store it unstyled so the modern stylesheet is applied on the fly
      # at render time (and the personalized/base stylesheet check stays valid).
      unstyled = re.sub(r"<style\b[^>]*>.*?</style>\s*", "", canonical_html, flags=re.IGNORECASE | re.DOTALL)
      base_cv.write_text(unstyled, encoding="utf-8")
    os.chmod(base_cv, 0o600)

  # A neutral plain-text starting draft the agent refines per job. Per-candidate
  # letters aren't available at claim time (one claim covers several jobs), so the
  # draft is the deterministic template shape; the agent tailors it to each job's
  # role/employer/requirements which travel with every candidate entry. Stored
  # UNSTYLED (plain text) with 0o600 perms, mirroring base-cv.html.
  base_letter = output_dir / "base-cover-letter.txt"
  if not base_letter.is_file():
    representative = work["candidates"][0] if work.get("candidates") else {}
    base_letter.write_text(
      cover_letter_text(work["subscriber"], work["preferences"], representative),
      encoding="utf-8",
    )
    os.chmod(base_letter, 0o600)

  public_work = {
    "schemaVersion": work["schemaVersion"],
    "runId": run_id,
    "claimId": args.claim_id,
    "preferences": work["preferences"],
    "applicationLimit": work["applicationLimit"],
    "matchThreshold": work["matchThreshold"],
    "candidates": work["candidates"],
    "preparationError": work.get("preparationError"),
    "baseCvHtmlPath": str(base_cv),
    "baseCoverLetterPath": str(base_letter),
    "coverLetterGuidance": COVER_LETTER_GUIDANCE,
    "personalizedOutputDirectory": str(personalized_dir),
    "resultPath": str(output_dir / "agent-result.json"),
  }
  return {"ok": True, "agentPhase": "claim", "work": public_work}


def _read_result_arg(value: str) -> dict:
  text = str(value or "").strip()
  if text.startswith("@"):
    path = Path(text[1:]).expanduser()
    if not path.is_file():
      raise AgentResultError("agent result file does not exist")
    if path.stat().st_size > 512 * 1024:
      raise AgentResultError("agent result is too large")
    text = path.read_text(encoding="utf-8")
  try:
    result = json.loads(text)
  except json.JSONDecodeError as err:
    raise AgentResultError("agent result is not valid JSON") from err
  if not isinstance(result, dict):
    raise AgentResultError("agent result must be an object")
  return result


def _safe_output_file(root: Path, value: object, max_bytes: int, suffix: str) -> Path:
  text = str(value or "").strip()
  if not text:
    raise AgentResultError(f"personalized {suffix} path is missing")
  path = Path(text).expanduser()
  try:
    resolved = path.resolve(strict=True)
    allowed = root.resolve(strict=True)
    resolved.relative_to(allowed)
  except (OSError, ValueError) as err:
    raise AgentResultError("personalized artifact is outside its claim directory") from err
  if path.is_symlink() or not resolved.is_file():
    raise AgentResultError("personalized artifact must be a regular file")
  if resolved.suffix.casefold() != suffix:
    raise AgentResultError(f"personalized artifact must use {suffix}")
  size = resolved.stat().st_size
  if size <= 0 or size > max_bytes:
    raise AgentResultError("personalized artifact has an invalid size")
  return resolved


class _CvHtmlInspector(HTMLParser):
  def __init__(self) -> None:
    super().__init__(convert_charrefs=True)
    self.unsafe = False
    self.styles: list[str] = []
    self.stylesheets: list[tuple[tuple[str, str], ...]] = []
    self.immutable: list[tuple[str, str]] = []
    self._style_parts: list[str] | None = None
    self._immutable_name: str | None = None
    self._immutable_parts: list[str] | None = None
    self._immutable_depth = 0

  def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
    name = tag.casefold()
    attributes = {str(key).casefold(): str(value or "") for key, value in attrs}
    if name in {"script", "iframe", "object", "embed"} or any(key.startswith("on") for key in attributes):
      self.unsafe = True
    if name == "style":
      self._style_parts = []
    if name == "link" and "stylesheet" in attributes.get("rel", "").casefold().split():
      self.stylesheets.append(tuple(sorted(attributes.items())))
    immutable_name = attributes.get("data-cv-immutable")
    field_name = attributes.get("data-cv-field", "").casefold()
    if not immutable_name and field_name not in {"identity", "contact", "employer", "job-title", "date", "education", "certification", "achievement"}:
      field_name = ""
    marker = immutable_name or field_name
    if name in VOID_ELEMENTS:
      if marker:
        self.immutable.append((marker, ""))
      return
    if self._immutable_parts is not None:
      self._immutable_depth += 1
    if marker:
      self._immutable_name = marker
      self._immutable_parts = []
      self._immutable_depth = 1

  def handle_endtag(self, tag: str) -> None:
    name = tag.casefold()
    if name == "style" and self._style_parts is not None:
      self.styles.append("".join(self._style_parts).strip())
      self._style_parts = None
    if name in VOID_ELEMENTS:
      return
    if self._immutable_parts is not None:
      self._immutable_depth -= 1
      if self._immutable_depth == 0:
        value = re.sub(r"\s+", " ", "".join(self._immutable_parts)).strip()
        self.immutable.append((str(self._immutable_name or ""), value))
        self._immutable_name = None
        self._immutable_parts = None

  def handle_data(self, data: str) -> None:
    if self._style_parts is not None:
      self._style_parts.append(data)
    if self._immutable_parts is not None:
      self._immutable_parts.append(data)


def _inspect_html(path: Path) -> _CvHtmlInspector:
  inspector = _CvHtmlInspector()
  try:
    inspector.feed(path.read_text(encoding="utf-8"))
    inspector.close()
  except (OSError, UnicodeDecodeError) as err:
    raise AgentResultError("personalized CV is not valid UTF-8 HTML") from err
  return inspector


def _validate_personalized_html(base_path: Path, personalized_path: Path) -> None:
  base = _inspect_html(base_path)
  personalized = _inspect_html(personalized_path)
  if not base.immutable:
    raise AgentResultError("canonical CV has no immutable fact markers")
  if personalized.unsafe:
    raise AgentResultError("personalized CV contains unsafe active HTML")
  if base.styles != personalized.styles or base.stylesheets != personalized.stylesheets:
    raise AgentResultError("personalized CV changed the canonical stylesheet or layout links")
  if base.immutable != personalized.immutable:
    raise AgentResultError("personalized CV changed an immutable fact")


def _letter_is_tailored(letter_path: Path, candidate: dict) -> bool:
  """Whether the agent's letter is a genuine, tailored plain-text letter.

  Returns False (rather than raising) so a lazy/empty/unfilled letter is treated
  as absent and dispatch_agent_user falls back to write_cover_letter — a single
  weak letter must not abort the whole validated result the way AgentResultError
  would. Size/suffix/sandbox is already enforced by _safe_output_file upstream.
  """
  try:
    text = letter_path.read_text(encoding="utf-8")
  except (OSError, UnicodeDecodeError):
    return False
  stripped = text.strip()
  if len(stripped) < MIN_LETTER_CHARS:
    return False
  if len([line for line in text.splitlines() if line.strip()]) < MIN_LETTER_LINES:
    return False
  # Plain text only: an HTML fragment means the agent ignored the contract.
  if re.search(r"<[a-zA-Z/][^>]*>", text):
    return False
  # Reject an unfilled template by requiring the letter to name the actual job:
  # its role or employer must appear so a generic placeholder is caught.
  haystack = stripped.casefold()
  role = str(candidate.get("role") or "").strip().casefold()
  company = str(candidate.get("company") or "").strip().casefold()
  company_known = bool(company) and company != UNKNOWN_EMPLOYER.casefold()
  role_ok = bool(role) and role in haystack
  company_ok = company_known and company in haystack
  return role_ok or company_ok


def validate_agent_result(args: argparse.Namespace, work: dict, result: dict) -> list[dict]:
  if result.get("schemaVersion") != AGENT_RESULT_SCHEMA_VERSION:
    raise AgentResultError(f"agent result schemaVersion must be {AGENT_RESULT_SCHEMA_VERSION}")
  if str(result.get("runId") or "") != safe_run_id(args.run_id) or str(result.get("claimId") or "") != args.claim_id:
    raise AgentResultError("agent result does not belong to this claim")
  assessments = result.get("assessments")
  if not isinstance(assessments, list):
    raise AgentResultError("agent result assessments must be an array")
  candidates = {candidate["agentCandidateId"]: candidate for candidate in work["candidates"]}
  seen: set[str] = set()
  used_paths: set[Path] = set()
  selected: list[dict] = []
  threshold = int(work.get("matchThreshold") or AGENT_MATCH_THRESHOLD)
  output_root = dispatch_store.agent_claim_output_dir(args.dispatch_root, args.run_id, args.claim_id) / "personalized"
  base_path = dispatch_store.agent_claim_output_dir(args.dispatch_root, args.run_id, args.claim_id) / "base-cv.html"

  for assessment in assessments:
    if not isinstance(assessment, dict):
      raise AgentResultError("each assessment must be an object")
    candidate_id = str(assessment.get("candidateId") or "")
    if candidate_id not in candidates or candidate_id in seen:
      raise AgentResultError("agent result contains an unknown or duplicate candidate")
    seen.add(candidate_id)
    score = assessment.get("fitScore")
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
      raise AgentResultError("fitScore must be an integer from 0 to 100")
    if not isinstance(assessment.get("hardMismatch"), bool) or not isinstance(assessment.get("selected"), bool):
      raise AgentResultError("hardMismatch and selected must be booleans")
    reason = public_text(assessment.get("reason"), 300)
    if not reason:
      raise AgentResultError("each assessment needs a concise reason")
    if not assessment["selected"]:
      continue
    if score < threshold or assessment["hardMismatch"]:
      raise AgentResultError("selected candidate does not meet the strong-fit policy")
    cv_path = _safe_output_file(output_root, assessment.get("cvHtmlPath"), MAX_HTML_BYTES, ".html")
    letter_path = _safe_output_file(output_root, assessment.get("coverLetterPath"), MAX_LETTER_BYTES, ".txt")
    # Shared artifact files would break later candidates once the first
    # success deletes the retained personalized HTML.
    if cv_path in used_paths or letter_path in used_paths:
      raise AgentResultError("each selected candidate needs its own artifact files")
    used_paths.update({cv_path, letter_path})
    _validate_personalized_html(base_path, cv_path)
    # A weak/empty/HTML/placeholder letter is dropped (not fatal) so dispatch
    # falls back to the deterministic template for this candidate while still
    # using the validated personalized CV.
    tailored_letter = letter_path if _letter_is_tailored(letter_path, candidates[candidate_id]) else None
    selected.append({
      "candidate": candidates[candidate_id],
      "reason": reason,
      "fitScore": score,
      "cvHtmlPath": cv_path,
      "coverLetterPath": tailored_letter,
    })

  if seen != set(candidates):
    raise AgentResultError("agent must assess every shortlisted candidate exactly once")
  if len(selected) > int(work.get("applicationLimit") or 0):
    raise AgentResultError("agent selected more jobs than the subscriber limit")
  return selected


def _record_outcome(root: Path, run_id: str, work: dict, result: dict) -> None:
  with dispatch_store.agent_state_lock(root):
    claim = dispatch_store.load_agent_claim(root, run_id, work["claimId"])
    if claim.get("status") in TERMINAL_CLAIM_STATUSES:
      return
    cmd_record_dispatch_outcome(argparse.Namespace(
      run_id=run_id,
      user_id=work["userId"],
      status=result["status"],
      applied_count=result["appliedCount"],
      action_required_count=result["actionRequiredCount"],
      dispatch_root=root,
    ))
    claim.update({
      "status": "fallback_completed" if result.get("fallback") else "completed",
      "completedAt": utc_now(),
      "outcome": {
        "status": result["status"],
        "appliedCount": result["appliedCount"],
        "actionRequiredCount": result["actionRequiredCount"],
        "fallback": bool(result.get("fallback")),
      },
    })
    dispatch_store.save_agent_claim(root, run_id, work["claimId"], claim)
    marker = dispatch_store.load_dispatch(run_id, root)
    marker["phase"] = "running"
    marker["updatedAt"] = utc_now()
    dispatch_store.save_dispatch(marker, root)


def _stored_documents(subscriber: dict, record_result: dict, attempt: int, documents: dict, web_result: dict | None = None) -> dict:
  store = getattr(application, "store_application_artifacts", None)
  if not callable(store):
    return {}
  cover_letter_pdf = documents.get("coverLetterPdf")
  if web_result is not None:
    evidence = web_result.get("evidence") if isinstance(web_result.get("evidence"), dict) else {}
    filled = evidence.get("filledFields") if isinstance(evidence.get("filledFields"), list) else []
    if "coverLetterFile" not in filled:
      cover_letter_pdf = None
  try:
    return store(
      subscriber,
      record_result,
      attempt,
      documents["cvPdf"],
      documents["coverLetterText"],
      cover_letter_pdf,
    )
  except Exception:
    # The application was already submitted and recorded. Artifact retention is
    # important but must not turn a successful external submission into a retry.
    return {}


def _remove_retained_personalized_html(path: object, artifact_refs: dict) -> None:
  """Drop editable tailored HTML only after the submitted PDF is in R2."""
  if not path or not artifact_refs:
    return
  try:
    Path(path).unlink()
  except OSError:
    pass


def _prepare_documents(
  subscriber: dict,
  candidate: dict,
  cover_letter: Path,
  personalized_html: Path | None,
  attempt: int,
  include_cover_letter_pdf: bool,
) -> dict:
  prepare = getattr(application, "prepare_application_documents", None)
  if callable(prepare):
    return prepare(
      subscriber,
      candidate,
      cover_letter,
      personalized_cv_html=personalized_html,
      include_cover_letter_pdf=include_cover_letter_pdf,
      attempt=attempt,
    )
  # Backward compatibility during rolling deployment; delivery keeps using the
  # legacy canonical attachment until the renderer module is present.
  return {"coverLetterText": cover_letter, "cvPdf": None, "coverLetterPdf": None}


def dispatch_agent_user(args: argparse.Namespace, work: dict, selected: list[dict], fallback: bool, fallback_reason: str | None = None) -> dict:
  subscriber = work["subscriber"]
  user_id = work["userId"]
  preferences = work["preferences"]
  applications = applications_from_response(cmd_list_applications(argparse.Namespace(user_id=user_id)))
  auto_apply = preferences.get("autoApply", True) is not False
  allow_browser = browser_apply_enabled(args)
  user_applied: list[dict] = []
  user_action_required: list[dict] = []
  failed_note = work.get("preparationError")
  channel_outcomes: set[str] = set()

  def _note_channel_outcome(candidate: dict, outcome: str, attempts: int) -> None:
    record_channel_candidate_outcome(args.run_id, user_id, candidate, outcome, attempts)
    channel_outcomes.add(str(candidate.get("agentCandidateId") or candidate_key(candidate)))

  for item in selected:
    candidate = item["candidate"]
    # A sibling/recovered run may have recorded the same company/role after the
    # packet was prepared. Re-run terminal/attempt filtering immediately before
    # any external side effect.
    eligible, _stats = filter_candidates_for_user_with_stats([candidate], preferences, applications, 1)
    if not eligible:
      continue
    reason = item.get("reason") or match_reason(preferences, candidate)
    attempt_count, replace_existing = candidate_attempt(applications, candidate)
    email = str(candidate.get("applicationEmail") or "").strip()
    application_url = candidate_application_url(candidate)
    personalized_html = item.get("cvHtmlPath")
    cover_letter = item.get("coverLetterPath") or write_cover_letter(subscriber, preferences, candidate)

    if email and auto_apply:
      if not gmail_sender_connected(subscriber):
        blocker = "gmail_not_connected"
        record_candidate_application(
          user_id, candidate, "action_required", notes=action_instruction(candidate, blocker), match=reason,
          submission_method="manual", application_url=application_url, blocker_code=blocker,
          attempt_count=attempt_count, replace=replace_existing,
        )
        _note_channel_outcome(candidate, "action_required", attempt_count)
        user_action_required.append({"candidate": candidate, "blockerCode": blocker})
        continue
      try:
        documents = _prepare_documents(
          subscriber,
          candidate,
          Path(cover_letter),
          Path(personalized_html) if personalized_html else None,
          attempt_count,
          include_cover_letter_pdf=True,
        )
        kwargs = {
          "cv_pdf": documents.get("cvPdf"),
          "cover_letter_pdf": documents.get("coverLetterPdf"),
        } if documents.get("cvPdf") else {}
        sent = send_application_email(subscriber, candidate, Path(cover_letter), **kwargs)
        recorded = record_candidate_application(
          user_id, candidate, "applied", message_id=result_message_id(sent), match=reason,
          submission_method="email", attempt_count=attempt_count, replace=replace_existing,
        )
        artifact_refs = _stored_documents(subscriber, recorded, attempt_count, documents)
        _remove_retained_personalized_html(personalized_html, artifact_refs)
        _note_channel_outcome(candidate, "applied", attempt_count)
        user_applied.append({"candidate": candidate, "reason": reason, "method": "email"})
        applications.append({**candidate, "status": "applied", "attemptCount": attempt_count})
      except Exception as err:
        failed_note = public_text(err)
        try:
          record_candidate_application(
            user_id, candidate, "failed", notes=failed_note, match=reason,
            submission_method="email", attempt_count=attempt_count, replace=replace_existing,
          )
        except Exception:
          pass
        _note_channel_outcome(candidate, "failed", attempt_count)
        break
    elif application_url and auto_apply and allow_browser:
      web_result: dict = {}
      try:
        documents = _prepare_documents(
          subscriber,
          candidate,
          Path(cover_letter),
          Path(personalized_html) if personalized_html else None,
          attempt_count,
          include_cover_letter_pdf=True,
        )
        payload_kwargs = {
          "cv_pdf": documents.get("cvPdf"),
          "cover_letter_pdf": documents.get("coverLetterPdf"),
        } if documents.get("cvPdf") else {}
        web_result = request_web_application(web_apply_payload(subscriber, preferences, candidate, Path(cover_letter), **payload_kwargs))
        if web_result["outcome"] == "submitted":
          recorded = record_candidate_application(
            user_id, candidate, "applied", notes=public_text(web_result.get("confirmation"), 300) or None,
            match=reason, submission_method="website", application_url=application_url,
            attempt_count=attempt_count, replace=replace_existing,
            evidence=web_result.get("evidence") if isinstance(web_result.get("evidence"), dict) else None,
          )
          artifact_refs = _stored_documents(subscriber, recorded, attempt_count, documents, web_result)
          _remove_retained_personalized_html(personalized_html, artifact_refs)
          _note_channel_outcome(candidate, "applied", attempt_count)
          user_applied.append({"candidate": candidate, "reason": reason, "method": "website"})
          applications.append({**candidate, "status": "applied", "attemptCount": attempt_count})
        elif web_result["outcome"] == "action_required":
          blocker = str(web_result.get("blockerCode") or "unsupported_site")
          record_candidate_application(
            user_id, candidate, "action_required", notes=action_instruction(candidate, blocker), match=reason,
            submission_method="website", application_url=application_url, blocker_code=blocker,
            attempt_count=attempt_count, replace=replace_existing,
          )
          _note_channel_outcome(candidate, "action_required", attempt_count)
          user_action_required.append({"candidate": candidate, "blockerCode": blocker})
        else:
          raise RuntimeError(public_text(web_result.get("error") or "website submission failed", 240))
      except Exception as err:
        failed_note = public_text(err)
        try:
          record_candidate_application(
            user_id, candidate, "failed", notes=failed_note, match=reason,
            submission_method="website", application_url=application_url,
            blocker_code=str(web_result.get("blockerCode") or "worker_failure"),
            attempt_count=attempt_count, replace=True,
          )
        except Exception:
          pass
        _note_channel_outcome(candidate, "failed", attempt_count)
        break
    elif application_url:
      blocker = "auto_apply_disabled"
      record_candidate_application(
        user_id, candidate, "action_required", notes=action_instruction(candidate, blocker), match=reason,
        submission_method="manual", application_url=application_url, blocker_code=blocker, attempt_count=0,
      )
      _note_channel_outcome(candidate, "action_required", 0)
      user_action_required.append({"candidate": candidate, "blockerCode": blocker})
    elif candidate.get("sourceChannelJid") and candidate.get("sourcePostId"):
      blocker = "channel_post_manual"
      record_candidate_application(
        user_id, candidate, "action_required", notes=action_instruction(candidate, blocker), match=reason,
        submission_method="manual", blocker_code=blocker, attempt_count=0,
      )
      _note_channel_outcome(candidate, "action_required", 0)
      user_action_required.append({"candidate": candidate, "blockerCode": blocker})

  # Eligibility was registered for the full shortlist at prepare time, and
  # zimlinks.finalize_dispatch only purges a post once every eligible user has
  # a terminal outcome — so candidates this run did not process are skipped.
  for candidate in work["candidates"]:
    if not candidate.get("sourceChannelJid") or not candidate.get("sourcePostId"):
      continue
    if str(candidate.get("agentCandidateId") or candidate_key(candidate)) in channel_outcomes:
      continue
    _note_channel_outcome(candidate, "skipped", 0)

  if user_applied:
    status = "applied"
  elif user_action_required:
    status = "action_required"
  elif failed_note:
    status = "failed"
  else:
    status = "no_matches"
  delivered = False
  try:
    date_label = datetime.now(timezone.utc).date().isoformat()
    send_whatsapp_summary(subscriber_phone(subscriber), build_user_summary(date_label, user_applied, user_action_required, failed_note))
    delivered = True
  except Exception as err:
    failed_note = public_text(err)
    status = "failed"
  publish_user_dashboard_telemetry(
    user_id, status, len(user_applied) if status != "failed" else 0,
    len(user_action_required) if status != "failed" else 0, delivered, failed_note,
  )
  return {
    "ok": True,
    "runId": args.run_id,
    "claimId": work["claimId"],
    "status": status,
    "appliedCount": len(user_applied) if status != "failed" else 0,
    "actionRequiredCount": len(user_action_required) if status != "failed" else 0,
    "fallback": fallback,
    "fallbackReason": public_text(fallback_reason, 240) if fallback_reason else None,
  }


def _begin_completion(args: argparse.Namespace) -> tuple[dict, dict | None]:
  run_id = safe_run_id(args.run_id)
  with dispatch_store.agent_state_lock(args.dispatch_root):
    claim = dispatch_store.load_agent_claim(args.dispatch_root, run_id, args.claim_id)
    if claim.get("status") in TERMINAL_CLAIM_STATUSES:
      return claim, claim.get("outcome") if isinstance(claim.get("outcome"), dict) else {}
    if claim.get("status") == "completing":
      # A completer that died mid-flight leaves the claim "completing" forever;
      # allow takeover once the attempt is clearly dead. Duplicate sends are
      # prevented by the terminal-status re-filter in dispatch_agent_user.
      age = dispatch_store.iso_age_seconds(claim.get("completionStartedAt"))
      if age is not None and age <= AGENT_COMPLETION_STALE_SECONDS:
        raise SystemExit("Job Scout claim completion is already in progress")
    elif claim.get("status") not in {"pending", "claimed"}:
      raise SystemExit("Job Scout claim cannot be completed")
    claim["status"] = "completing"
    claim["completionStartedAt"] = utc_now()
    dispatch_store.save_agent_claim(args.dispatch_root, run_id, args.claim_id, claim)
    return claim, None


def cmd_agent_complete(args: argparse.Namespace) -> dict:
  # Load the work packet before flipping the claim to "completing" so a failing
  # load cannot strand the claim without a recorded outcome.
  work = dispatch_store.load_agent_work(args.dispatch_root, args.run_id, args.claim_id)
  claim, existing = _begin_completion(args)
  if existing is not None:
    return {"ok": True, "agentPhase": "complete", "duplicate": True, **existing}
  try:
    selected = validate_agent_result(args, work, _read_result_arg(args.result))
    outcome = dispatch_agent_user(args, work, selected, fallback=False)
  except (AgentResultError, OSError, UnicodeError) as err:
    fallback_selected = [
      {"candidate": candidate, "reason": match_reason(work["preferences"], candidate)}
      for candidate in work["candidates"][: int(work.get("applicationLimit") or 0)]
    ]
    try:
      outcome = dispatch_agent_user(args, work, fallback_selected, fallback=True, fallback_reason=str(err))
    except (Exception, SystemExit) as fallback_err:
      outcome = {
        "ok": True,
        "runId": args.run_id,
        "claimId": work["claimId"],
        "status": "failed",
        "appliedCount": 0,
        "actionRequiredCount": 0,
        "fallback": True,
        "fallbackReason": public_text(fallback_err, 240),
      }
  except (Exception, SystemExit) as err:
    outcome = {
      "ok": True,
      "runId": args.run_id,
      "claimId": work["claimId"],
      "status": "failed",
      "appliedCount": 0,
      "actionRequiredCount": 0,
      "fallback": True,
      "fallbackReason": public_text(err, 240),
    }
  _record_outcome(args.dispatch_root, args.run_id, work, outcome)
  return {"agentPhase": "complete", **outcome}


def cmd_agent_fallback(args: argparse.Namespace) -> dict:
  work = dispatch_store.load_agent_work(args.dispatch_root, args.run_id, args.claim_id)
  claim, existing = _begin_completion(args)
  if existing is not None:
    return {"ok": True, "agentPhase": "fallback", "duplicate": True, **existing}
  selected = [
    {"candidate": candidate, "reason": match_reason(work["preferences"], candidate)}
    for candidate in work["candidates"][: int(work.get("applicationLimit") or 0)]
  ]
  try:
    outcome = dispatch_agent_user(
      args, work, selected, fallback=True,
      fallback_reason=getattr(args, "fallback_reason", None) or "OpenClaw child failed",
    )
  except (Exception, SystemExit) as err:
    outcome = {
      "ok": True,
      "runId": args.run_id,
      "claimId": work["claimId"],
      "status": "failed",
      "appliedCount": 0,
      "actionRequiredCount": 0,
      "fallback": True,
      "fallbackReason": public_text(err, 240),
    }
  _record_outcome(args.dispatch_root, args.run_id, work, outcome)
  return {"agentPhase": "fallback", **outcome}


def cmd_agent_status(args: argparse.Namespace) -> dict:
  run_id = safe_run_id(args.run_id)
  claims = _load_claims(args.dispatch_root, run_id)
  return {
    "ok": True,
    "agentPhase": "status",
    "runId": run_id,
    "claimCount": len(claims),
    "terminalCount": sum(claim.get("status") in TERMINAL_CLAIM_STATUSES for claim in claims),
    "claims": [_claim_summary(claim) for claim in claims],
  }


def cmd_agent_finish(args: argparse.Namespace) -> dict:
  run_id = safe_run_id(args.run_id)
  with dispatch_store.agent_state_lock(args.dispatch_root):
    marker = dispatch_store.load_dispatch(run_id, args.dispatch_root)
    if marker.get("finishedAt"):
      return {"ok": True, "agentPhase": "finish", "runId": run_id, "duplicate": True, "finalCounts": marker.get("finalCounts")}
    claims = _load_claims(args.dispatch_root, run_id)
    unfinished = [_claim_summary(claim) for claim in claims if claim.get("status") not in TERMINAL_CLAIM_STATUSES]
    if unfinished:
      raise SystemExit(f"Job Scout agent dispatch has {len(unfinished)} unfinished claim(s)")
    outcomes = [claim.get("outcome") if isinstance(claim.get("outcome"), dict) else {} for claim in claims]
    counts = {
      "ready_count": len(claims),
      "processed_count": len(claims),
      "applied_count": sum(int(outcome.get("appliedCount") or 0) for outcome in outcomes),
      "action_required_count": sum(int(outcome.get("actionRequiredCount") or 0) for outcome in outcomes),
      "failed_count": sum(outcome.get("status") == "failed" for outcome in outcomes),
      "no_matches_count": sum(outcome.get("status") == "no_matches" for outcome in outcomes),
    }
    cmd_finish_dispatch(argparse.Namespace(
      run_id=run_id,
      dispatch_root=args.dispatch_root,
      physical_count=0,
      **counts,
    ))
    cmd_verify_dispatch(argparse.Namespace(run_id=run_id, dispatch_root=args.dispatch_root))
    dispatch_store.clear_active_agent_dispatch(args.dispatch_root, run_id)
  finalize_channel_candidate_dispatch(run_id)
  marker = dispatch_store.load_dispatch(run_id, args.dispatch_root)
  return {"ok": True, "agentPhase": "finish", "runId": run_id, "finalCounts": marker.get("finalCounts")}


def cmd_agent_dispatch(args: argparse.Namespace) -> dict:
  phase = str(getattr(args, "agent_phase", "") or "")
  if getattr(args, "dry_run", False):
    # Agent phases persist durable run state (marker, active pointer, channel
    # eligibility); there is no preview mode, so fail loudly instead of
    # silently starting a real run.
    raise SystemExit("--dry-run is not supported with --agent-phase; run dispatch-jsearch --dry-run without an agent phase")
  handlers = {
    "prepare": cmd_agent_prepare,
    "claim": cmd_agent_claim,
    "complete": cmd_agent_complete,
    "fallback": cmd_agent_fallback,
    "status": cmd_agent_status,
    "finish": cmd_agent_finish,
  }
  handler = handlers.get(phase)
  if handler is None:
    raise SystemExit("invalid Job Scout agent phase")
  if phase != "prepare" and not getattr(args, "run_id", None):
    raise SystemExit(f"--run-id is required for agent phase {phase}")
  if phase in {"claim", "complete", "fallback"} and not getattr(args, "claim_id", None):
    raise SystemExit(f"--claim-id is required for agent phase {phase}")
  if phase == "complete" and not getattr(args, "result", None):
    raise SystemExit("--result is required for agent phase complete")
  return handler(args)
