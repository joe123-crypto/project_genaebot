"""Application-artifact shaping for Job Scout.

Cover letters, the web-apply payload, application URL selection, attempt
counting, the user-facing dispatch summary, and action instructions. Pure
shaping over subscriber/candidate/preference data; imported by dispatch.
"""

from __future__ import annotations

from pathlib import Path

from .constants import (
  MAX_APPLICATION_ATTEMPTS,
  PUBLIC_DISPATCH_FAILURE_MESSAGE,
  UNKNOWN_EMPLOYER,
  WHATSAPP_CHANNEL_SOURCES,
)
from .jsearch import application_key, candidate_key
from . import api
from .commands_setup import style_cv_html
from .delivery import _html_source, cover_letter_html, render_html_to_pdf
from .profile import clean_items, profile_values, subscriber_user_id
from .subscriber import (
  applicant_name,
  application_artifact_dir,
  display_name,
  document_filename,
  subscriber_cv_html,
  subscriber_cv_file,
  subscriber_phone,
  subscriber_sender_email,
)
from .textutil import clean_job_title, natural_email_text, public_text


def applications_from_response(result: dict) -> list[dict]:
  applications = result.get("applications")
  if isinstance(applications, list):
    return [item for item in applications if isinstance(item, dict)]
  if isinstance(result, list):
    return [item for item in result if isinstance(item, dict)]
  return []


def candidate_attempt(applications: list[dict], candidate: dict) -> tuple[int, bool]:
  key = application_key(candidate)
  for application in applications:
    if application_key(application) != key:
      continue
    try:
      previous = int(application.get("attemptCount") or 0)
    except (TypeError, ValueError):
      previous = 0
    return min(previous + 1, MAX_APPLICATION_ATTEMPTS), True
  return 1, False


def match_reason(preferences: dict, candidate: dict) -> str:
  roles = clean_items(profile_values(preferences, "targetRoles") or profile_values(preferences, "roles"))
  role_hint = roles[0] if roles else str(candidate.get("role") or "the target role")
  return public_text(f"Matches requested work around {role_hint} and the configured location/preferences.", 220)


def clean_role(value: object) -> str:
  """A letter-safe job title, or a bare noun phrase when the role is unusable.

  Upstream sources sometimes put a description blurb in the role field (a whole
  "we are looking for ..." sentence), truncated mid-word. A genuine title is
  short, so anything long or multi-clause falls back to a generic phrase. The
  return value is a bare noun phrase so it reads correctly in "the {role}
  position" (never "the the advertised role position").
  """
  return clean_job_title(value) or "advertised role"


def cover_letter_text(subscriber: dict, preferences: dict, candidate: dict) -> str:
  name = display_name(subscriber)
  role = clean_role(candidate.get("role"))
  company = natural_email_text(candidate.get("company"), 120)
  # When the employer is unknown, never reference them anywhere in the letter.
  company_known = bool(company) and company != UNKNOWN_EMPLOYER
  qualifications = clean_items(profile_values(preferences, "qualifications"))
  skills = clean_items(profile_values(preferences, "skills"))
  experience = clean_items(profile_values(preferences, "experience"))
  strengths = ", ".join([*qualifications[:2], *skills[:3], *experience[:2]]) or "relevant experience and a strong work ethic"
  greeting = f"Dear Hiring Team at {company}," if company_known else "Dear Hiring Team,"
  opener = (
    f"I am applying for the {role} position at {company}."
    if company_known
    else f"I am applying for the {role} position."
  )
  return "\n".join([
    greeting,
    "",
    opener,
    f"My background includes {strengths}, and I am ready to contribute reliably from the first day.",
    "I work carefully, communicate clearly, and take responsibility for completing assigned tasks.",
    "I have attached my CV for your review and would appreciate the opportunity to discuss how I can help your team.",
    "",
    "Kind regards,",
    name,
  ]) + "\n"


def write_cover_letter(
  subscriber: dict,
  preferences: dict,
  candidate: dict,
  content: str | None = None,
  attempt: int | None = None,
) -> Path:
  directory = application_artifact_dir(subscriber, candidate, attempt)
  directory.mkdir(parents=True, exist_ok=True)
  path = directory / "cover-letter.txt"
  path.write_text(content if content is not None else cover_letter_text(subscriber, preferences, candidate), encoding="utf-8")
  return path


def prepare_application_documents(
  subscriber: dict,
  candidate: dict,
  cover_letter: str | Path,
  personalized_cv_html: str | Path | None = None,
  include_cover_letter_pdf: bool = False,
  attempt: int = 1,
) -> dict[str, Path]:
  """Render transient application content into the exact submit-ready files."""
  directory = application_artifact_dir(subscriber, candidate, attempt)
  directory.mkdir(parents=True, exist_ok=True)
  letter_path = Path(cover_letter).expanduser()
  if not letter_path.is_file() or not letter_path.read_text(encoding="utf-8").strip():
    raise RuntimeError("cover letter text is missing")
  cv_source = personalized_cv_html if personalized_cv_html is not None else subscriber_cv_html(subscriber)
  # The base/personalized CV is unstyled semantic HTML; apply the modern
  # stylesheet on the fly before rendering to PDF.
  styled_cv = style_cv_html(_html_source(cv_source))
  cv_pdf = render_html_to_pdf(styled_cv, directory / document_filename(subscriber, "cv"))
  documents = {
    "cvPdf": cv_pdf,
    "coverLetterText": letter_path,
  }
  if include_cover_letter_pdf:
    letter_html = cover_letter_html(letter_path.read_text(encoding="utf-8"))
    documents["coverLetterPdf"] = render_html_to_pdf(letter_html, directory / document_filename(subscriber, "cover_letter"))
  return documents


def _record_application_id(record_result: dict) -> str:
  for value in (record_result.get("id"), record_result.get("applicationId")):
    text = str(value or "").strip()
    if text:
      return text
  application = record_result.get("application")
  if isinstance(application, dict):
    text = str(application.get("id") or application.get("applicationId") or "").strip()
    if text:
      return text
  raise RuntimeError("recorded application response is missing its id")


def store_application_artifacts(
  subscriber: dict,
  record_result: dict,
  attempt: int,
  cv_pdf: str | Path,
  cover_letter_text: str | Path,
  cover_letter_pdf: str | Path | None = None,
) -> dict[str, str]:
  """Upload only the exact files/text used by a successful submission."""
  user_id = subscriber_user_id(subscriber)
  application_id = _record_application_id(record_result)
  artifacts: list[tuple[str, str, str | Path]] = [
    ("cv", "cv", cv_pdf),
    ("coverLetterText", "cover_letter_text", cover_letter_text),
  ]
  if cover_letter_pdf is not None:
    artifacts.append(("coverLetterPdf", "cover_letter_pdf", cover_letter_pdf))
  refs: dict[str, str] = {}
  for result_key, kind, source in artifacts:
    uploaded = api.upload_application_artifact(user_id, application_id, attempt, kind, source)
    ref = str(uploaded.get("artifactRef") or uploaded.get("key") or "").strip()
    if not ref:
      raise RuntimeError("application artifact upload returned no reference")
    refs[result_key] = ref
  return refs


def candidate_application_url(candidate: dict) -> str | None:
  links = candidate.get("applyLinks")
  if isinstance(links, list):
    for value in links:
      link = str(value or "").strip()
      if link.startswith(("https://", "http://")):
        return link
  # The channel permalink is provenance, not an application form.
  if str(candidate.get("source") or "") in WHATSAPP_CHANNEL_SOURCES or (
    candidate.get("sourceChannelJid") and candidate.get("sourcePostId")
  ):
    return None
  source = str(candidate.get("sourceUrl") or "").strip()
  return source if source.startswith(("https://", "http://")) else None


def web_apply_payload(
  subscriber: dict,
  preferences: dict,
  candidate: dict,
  cover_letter: Path,
  cv_pdf: Path | None = None,
  cover_letter_pdf: Path | None = None,
) -> dict:
  first_name, last_name = applicant_name(subscriber)
  locations = clean_items(profile_values(preferences, "locations"))
  resolved_cv = cv_pdf
  resolved_letter_pdf = cover_letter_pdf
  if resolved_cv is None:
    legacy_cv = Path(subscriber_cv_file(subscriber))
    if legacy_cv.suffix.casefold() == ".pdf":
      resolved_cv = legacy_cv
    else:
      documents = prepare_application_documents(
        subscriber,
        candidate,
        cover_letter,
        include_cover_letter_pdf=resolved_letter_pdf is None,
      )
      resolved_cv = documents["cvPdf"]
      resolved_letter_pdf = resolved_letter_pdf or documents.get("coverLetterPdf")
  if resolved_letter_pdf is None:
    directory = application_artifact_dir(subscriber, candidate)
    letter_html = cover_letter_html(Path(cover_letter).read_text(encoding="utf-8"))
    resolved_letter_pdf = render_html_to_pdf(letter_html, directory / document_filename(subscriber, "cover_letter"))
  return {
    "attemptId": candidate_key(candidate),
    "applicationUrl": candidate_application_url(candidate),
    "applicant": {
      "firstName": first_name,
      "lastName": last_name,
      "fullName": display_name(subscriber),
      "email": subscriber_sender_email(subscriber),
      "phone": subscriber_phone(subscriber),
      "location": locations[0] if locations else "",
    },
    "cvPath": str(resolved_cv),
    "coverLetterTextPath": str(cover_letter),
    "coverLetterFilePath": str(resolved_letter_pdf),
    "autoSubmit": True,
  }


def action_instruction(candidate: dict, blocker_code: str | None = None) -> str:
  link = candidate_application_url(candidate) or str(candidate.get("sourcePostUrl") or candidate.get("sourceUrl") or "").strip()
  if str(blocker_code or "") == "auto_apply_disabled" and link:
    return f"Apply manually here: {link}"
  reasons = {
    "login_required": "The site requires you to sign in.",
    "captcha_required": "The site requires a CAPTCHA.",
    "unknown_required_field": "The form asks a required question I cannot answer safely.",
    "legal_declaration": "The form requires a declaration only you should confirm.",
    "payment_required": "The site asks for payment; I will never submit or pay it.",
    "unsupported_site": "This site's application form is not safely automatable yet.",
    "auto_apply_disabled": "Apply manually here.",
    "channel_post_manual": "The channel post has no application form or email. Review the original advert and apply manually.",
    "gmail_not_connected": "Reconnect Gmail before I can email this application for you.",
  }
  prefix = reasons.get(str(blocker_code or ""), "This application needs your input.")
  if link:
    return f"{prefix} Continue here: {link}"
  return prefix


def build_user_summary(date_label: str, applied: list[dict], action_required: list[dict], failed_note: str | None = None) -> str:
  lines = [f"Job Scout - {date_label}", ""]
  if applied:
    for index, item in enumerate(applied, 1):
      candidate = item["candidate"]
      lines.extend([
        f"{index}. {clean_role(candidate.get('role'))} - APPLIED",
        f"Company: {public_text(candidate.get('company'), 100)}",
      ])
      location = public_text(candidate.get("location"), 100)
      if location:
        lines.append(f"Location: {location}")
      source = public_text(candidate.get("sourceUrl"), 180)
      if source:
        lines.append(f"Source: {source}")
      reason = public_text(item.get("reason"), 180)
      if reason:
        lines.append(f"Why: {reason}")
      lines.append("")
  if action_required:
    for item in action_required:
      candidate = item["candidate"] if isinstance(item, dict) and "candidate" in item else item
      blocker_code = item.get("blockerCode") if isinstance(item, dict) else None
      lines.extend([
        f"- {clean_role(candidate.get('role'))} - ACTION NEEDED",
        f"Company: {public_text(candidate.get('company'), 100)}",
        action_instruction(candidate, blocker_code),
        "",
      ])
  if failed_note:
    lines.append(PUBLIC_DISPATCH_FAILURE_MESSAGE)
  if not applied and not action_required and not failed_note:
    lines.append("No strong matches today. I will keep checking.")
  return "\n".join(line for line in lines).strip()
