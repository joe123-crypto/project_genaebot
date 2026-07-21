#!/usr/bin/env python3

from __future__ import annotations

import argparse
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from jobscout import agent_dispatch, dispatch_store


class AgentDispatchTests(unittest.TestCase):
  def subscriber(self, user_id: str = "user-1") -> dict:
    return {
      "userId": user_id,
      "whatsappPhone": "+263700000001",
      "senderEmail": "applicant@example.com",
      "gmailConnected": True,
      "ready": True,
      "cvFileRef": f"{user_id}/cv/base/cv.html",
      "cvHtmlRef": f"{user_id}/cv/base/cv.html",
      "cvConversionStatus": "ready",
      "profile": {"displayName": "Test Applicant"},
      "preferences": {
        "targetRoles": ["Driver"],
        "locations": ["Harare, Zimbabwe"],
        "country": "zw",
        "language": "en",
        "maxApplicationsPerRun": 1,
      },
    }

  def candidate(self) -> dict:
    return {
      "candidateId": "source-job-1",
      "source": "jsearch",
      "sourceUrl": "https://jobs.example/driver",
      "applyLinks": ["https://jobs.example/driver"],
      "applicationEmail": "jobs@example.com",
      "company": "Example Logistics",
      "role": "Driver",
      "location": "Harare, Zimbabwe",
      "description": "Deliver parcels safely.",
    }

  def args(self, root: Path, phase: str, run_id: str | None = "agent-test", claim_id: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
      agent_phase=phase,
      run_id=run_id,
      claim_id=claim_id,
      result=None,
      fallback_reason=None,
      phone=None,
      limit_ready=100,
      max_candidates_per_user=5,
      shortlist_limit=20,
      match_threshold=70,
      dispatch_root=root,
      dry_run=False,
      write_group_feed_cache=False,
      group_feed_root=root / "group-feed",
      group_feed_limit=80,
      browser_apply=False,
    )

  def prepare(self, root: Path) -> dict:
    with (
      mock.patch.object(agent_dispatch, "cmd_list_ready", return_value={"subscribers": [self.subscriber()]}),
      mock.patch.object(agent_dispatch, "cmd_list_applications", return_value={"applications": []}),
      mock.patch.object(
        agent_dispatch,
        "fetch_candidate_sources",
        return_value=([self.candidate()], {
          "jsearch": {"status": "ok", "rawCount": 1, "durationMs": 1},
        }),
      ),
      mock.patch.object(agent_dispatch, "register_channel_candidate_eligibility"),
    ):
      return agent_dispatch.cmd_agent_prepare(self.args(root, "prepare"))

  def test_prepare_creates_private_claim_and_resumes_active_run(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      first = self.prepare(root)
      claim_id = first["claims"][0]["claimId"]
      claim_path = dispatch_store.agent_claim_path(root, "agent-test", claim_id)
      work_path = dispatch_store.agent_work_path(root, "agent-test", claim_id)
      second_args = self.args(root, "prepare", run_id="different-run")
      second = agent_dispatch.cmd_agent_prepare(second_args)

      self.assertFalse(first["resumed"])
      self.assertTrue(second["resumed"])
      self.assertEqual(second["runId"], "agent-test")
      self.assertEqual(claim_path.stat().st_mode & 0o777, 0o600)
      self.assertEqual(work_path.stat().st_mode & 0o777, 0o600)

  def test_claim_is_idempotent_and_materializes_canonical_html(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      args = self.args(root, "claim", claim_id=claim_id)
      # A current, unstyled semantic base CV (carries the template marker and no
      # inline stylesheet) is materialized verbatim.
      html = '<!doctype html><main class="cv" data-cv-template="professional-v2"><p data-cv-immutable="name">Test Applicant</p></main>'
      canonical = root / "canonical.html"
      canonical.write_text(html, encoding="utf-8")
      with mock.patch.object(agent_dispatch, "subscriber_cv_html", return_value=str(canonical)):
        first = agent_dispatch.cmd_agent_claim(args)
        second = agent_dispatch.cmd_agent_claim(args)

      base = Path(first["work"]["baseCvHtmlPath"])
      self.assertEqual(base.read_text(encoding="utf-8"), html)
      self.assertEqual(first["work"]["claimId"], second["work"]["claimId"])
      self.assertEqual(len(first["work"]["candidates"]), 1)

  def test_claim_rebuilds_legacy_generated_cv_from_original_pdf(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      args = self.args(root, "claim", claim_id=claim_id)
      canonical = root / "canonical.html"
      canonical.write_text(
        "<style>body { font-family: Georgia, 'Times New Roman', serif; }</style><main>Old CV</main>",
        encoding="utf-8",
      )

      def download(_user_id, destination, format="html"):
        self.assertEqual(format, "pending")
        destination.write_bytes(b"%PDF-original")
        return destination

      def convert(source, destination):
        self.assertTrue(source.is_file())
        destination.write_text(
          '<main class="cv" data-cv-template="professional-v2"><p data-cv-field="identity">Applicant</p></main>',
          encoding="utf-8",
        )
        return destination

      with mock.patch.object(agent_dispatch, "subscriber_cv_html", return_value=str(canonical)), mock.patch.object(
        agent_dispatch.api,
        "download_cv",
        side_effect=download,
      ) as fetch, mock.patch.object(agent_dispatch, "convert_pdf_to_cv_html", side_effect=convert) as rebuild:
        claimed = agent_dispatch.cmd_agent_claim(args)

      base = Path(claimed["work"]["baseCvHtmlPath"])
      self.assertIn('data-cv-template="professional-v2"', base.read_text(encoding="utf-8"))
      self.assertFalse((base.parent / "base-source.pdf").exists())
      fetch.assert_called_once()
      rebuild.assert_called_once()

  def test_validator_accepts_strong_fit_and_preserved_html(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      args = self.args(root, "claim", claim_id=claim_id)
      # Unstyled semantic base CV; the agent edits only mutable content.
      html = '<!doctype html><main class="cv" data-cv-template="professional-v2"><p data-cv-immutable="name">Test Applicant</p><p>Summary</p></main>'
      canonical = root / "canonical.html"
      canonical.write_text(html, encoding="utf-8")
      with mock.patch.object(agent_dispatch, "subscriber_cv_html", return_value=str(canonical)):
        claimed = agent_dispatch.cmd_agent_claim(args)
      candidate_id = claimed["work"]["candidates"][0]["agentCandidateId"]
      artifact_dir = Path(claimed["work"]["personalizedOutputDirectory"]) / "job"
      artifact_dir.mkdir()
      cv = artifact_dir / "cv.html"
      cv.write_text(html.replace("Summary", "Driver summary"), encoding="utf-8")
      letter = artifact_dir / "cover-letter.txt"
      letter.write_text(
        "Dear Hiring Team at Example Logistics,\n\n"
        "I am applying for the Driver position at Example Logistics.\n"
        "My background includes safe parcel delivery and a clean driving record,\n"
        "and I am ready to contribute reliably from the first day.\n\n"
        "Kind regards,\nTest Applicant\n",
        encoding="utf-8",
      )
      work = dispatch_store.load_agent_work(root, "agent-test", claim_id)
      complete_args = self.args(root, "complete", claim_id=claim_id)
      result = {
        "schemaVersion": 1,
        "runId": "agent-test",
        "claimId": claim_id,
        "assessments": [{
          "candidateId": candidate_id,
          "fitScore": 84,
          "hardMismatch": False,
          "selected": True,
          "reason": "Driving experience and location align.",
          "cvHtmlPath": str(cv),
          "coverLetterPath": str(letter),
        }],
      }

      selected = agent_dispatch.validate_agent_result(complete_args, work, result)
      self.assertEqual(selected[0]["fitScore"], 84)
      # A genuine tailored letter is retained (not dropped to the template).
      self.assertEqual(selected[0]["coverLetterPath"], letter)

  def test_claim_packet_includes_cover_letter_guidance_and_base_draft(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      args = self.args(root, "claim", claim_id=claim_id)
      html = '<!doctype html><main class="cv" data-cv-template="professional-v2"><p data-cv-immutable="name">Test Applicant</p></main>'
      canonical = root / "canonical.html"
      canonical.write_text(html, encoding="utf-8")
      with mock.patch.object(agent_dispatch, "subscriber_cv_html", return_value=str(canonical)):
        claimed = agent_dispatch.cmd_agent_claim(args)
      work = claimed["work"]
      self.assertIn("coverLetterGuidance", work)
      self.assertTrue(work["coverLetterGuidance"])
      base_letter = Path(work["baseCoverLetterPath"])
      self.assertTrue(base_letter.is_file())
      self.assertEqual(base_letter.stat().st_mode & 0o777, 0o600)
      draft = base_letter.read_text(encoding="utf-8")
      self.assertIn("Driver", draft)
      # The base draft is plain text, never HTML.
      self.assertNotRegex(draft, r"<[a-zA-Z/]")

  def test_letter_quality_validator_accepts_and_rejects(self) -> None:
    candidate = {"role": "Driver", "company": "Example Logistics"}
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      good = root / "good.txt"
      good.write_text(
        "Dear Hiring Team at Example Logistics,\n\n"
        "I am applying for the Driver position at Example Logistics.\n"
        "My background includes safe parcel delivery and a clean record,\n"
        "and I am ready to contribute reliably from the first day.\n\n"
        "Kind regards,\nTest Applicant\n",
        encoding="utf-8",
      )
      self.assertTrue(agent_dispatch._letter_is_tailored(good, candidate))

      empty = root / "empty.txt"
      empty.write_text("   \n", encoding="utf-8")
      self.assertFalse(agent_dispatch._letter_is_tailored(empty, candidate))

      htmlish = root / "html.txt"
      htmlish.write_text(good.read_text(encoding="utf-8") + "<p>oops</p>\n", encoding="utf-8")
      self.assertFalse(agent_dispatch._letter_is_tailored(htmlish, candidate))

      placeholder = root / "placeholder.txt"
      placeholder.write_text(
        "Dear Hiring Team at [COMPANY],\n\n"
        "I am applying for the [ROLE] position at [COMPANY].\n"
        "My background includes relevant experience and a strong work ethic,\n"
        "and I am ready to contribute from the first day.\n\n"
        "Kind regards,\nApplicant\n",
        encoding="utf-8",
      )
      self.assertFalse(agent_dispatch._letter_is_tailored(placeholder, candidate))

  def test_weak_letter_is_dropped_so_dispatch_uses_template(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      args = self.args(root, "claim", claim_id=claim_id)
      html = '<!doctype html><main class="cv" data-cv-template="professional-v2"><p data-cv-immutable="name">Test Applicant</p><p>Summary</p></main>'
      canonical = root / "canonical.html"
      canonical.write_text(html, encoding="utf-8")
      with mock.patch.object(agent_dispatch, "subscriber_cv_html", return_value=str(canonical)):
        claimed = agent_dispatch.cmd_agent_claim(args)
      candidate_id = claimed["work"]["candidates"][0]["agentCandidateId"]
      artifact_dir = Path(claimed["work"]["personalizedOutputDirectory"]) / "job"
      artifact_dir.mkdir()
      cv = artifact_dir / "cv.html"
      cv.write_text(html.replace("Summary", "Driver summary"), encoding="utf-8")
      letter = artifact_dir / "cover-letter.txt"
      # Too short and does not name the role/company: a lazy/placeholder letter.
      letter.write_text("Please consider me.\n", encoding="utf-8")
      work = dispatch_store.load_agent_work(root, "agent-test", claim_id)
      result = {
        "schemaVersion": 1,
        "runId": "agent-test",
        "claimId": claim_id,
        "assessments": [{
          "candidateId": candidate_id,
          "fitScore": 84,
          "hardMismatch": False,
          "selected": True,
          "reason": "Driving experience and location align.",
          "cvHtmlPath": str(cv),
          "coverLetterPath": str(letter),
        }],
      }

      stderr = io.StringIO()
      with redirect_stderr(stderr):
        selected = agent_dispatch.validate_agent_result(self.args(root, "complete", claim_id=claim_id), work, result)
      # The CV survives, but the weak letter is dropped so dispatch falls back to
      # write_cover_letter for this candidate.
      self.assertEqual(selected[0]["cvHtmlPath"], cv)
      self.assertIsNone(selected[0]["coverLetterPath"])
      # The silent fallback is logged so degraded personalization is auditable.
      self.assertIn("failed the tailored-letter check", stderr.getvalue())
      self.assertIn(candidate_id, stderr.getvalue())

  def test_invalid_agent_result_immediately_uses_fallback_and_duplicate_is_safe(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      claim_args = self.args(root, "claim", claim_id=claim_id)
      canonical = root / "canonical.html"
      canonical.write_text("<html><style></style><p>Applicant</p></html>", encoding="utf-8")
      with mock.patch.object(agent_dispatch, "subscriber_cv_html", return_value=str(canonical)):
        agent_dispatch.cmd_agent_claim(claim_args)
      complete_args = self.args(root, "complete", claim_id=claim_id)
      complete_args.result = json.dumps({"schemaVersion": 1, "runId": "wrong", "claimId": claim_id, "assessments": []})
      fallback_outcome = {
        "ok": True,
        "runId": "agent-test",
        "claimId": claim_id,
        "status": "no_matches",
        "appliedCount": 0,
        "actionRequiredCount": 0,
        "fallback": True,
        "fallbackReason": "invalid",
      }
      with mock.patch.object(agent_dispatch, "dispatch_agent_user", return_value=fallback_outcome) as dispatch:
        first = agent_dispatch.cmd_agent_complete(complete_args)
        second = agent_dispatch.cmd_agent_complete(complete_args)

      self.assertTrue(first["fallback"])
      self.assertTrue(second["duplicate"])
      self.assertEqual(dispatch.call_count, 1)

  def test_validator_rejects_oversized_personalized_html(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      args = self.args(root, "claim", claim_id=claim_id)
      html = '<!doctype html><main class="cv" data-cv-template="professional-v2"><p data-cv-immutable="name">Test Applicant</p></main>'
      canonical = root / "canonical.html"
      canonical.write_text(html, encoding="utf-8")
      with mock.patch.object(agent_dispatch, "subscriber_cv_html", return_value=str(canonical)):
        claimed = agent_dispatch.cmd_agent_claim(args)
      candidate_id = claimed["work"]["candidates"][0]["agentCandidateId"]
      artifact_dir = Path(claimed["work"]["personalizedOutputDirectory"]) / "job"
      artifact_dir.mkdir()
      cv = artifact_dir / "cv.html"
      cv.write_text(html + "<p>" + "x" * agent_dispatch.MAX_HTML_BYTES + "</p>", encoding="utf-8")
      letter = artifact_dir / "cover-letter.txt"
      letter.write_text("A truthful tailored letter.\n", encoding="utf-8")
      work = dispatch_store.load_agent_work(root, "agent-test", claim_id)
      result = {
        "schemaVersion": 1,
        "runId": "agent-test",
        "claimId": claim_id,
        "assessments": [{
          "candidateId": candidate_id,
          "fitScore": 84,
          "hardMismatch": False,
          "selected": True,
          "reason": "Strong fit.",
          "cvHtmlPath": str(cv),
          "coverLetterPath": str(letter),
        }],
      }

      with self.assertRaisesRegex(agent_dispatch.AgentResultError, "invalid size"):
        agent_dispatch.validate_agent_result(self.args(root, "complete", claim_id=claim_id), work, result)

  def test_validator_rejects_shared_artifact_paths(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      second = {**self.candidate(), "candidateId": "source-job-2", "role": "Driver Assistant", "company": "Other Logistics"}
      with (
        mock.patch.object(agent_dispatch, "cmd_list_ready", return_value={"subscribers": [self.subscriber()]}),
        mock.patch.object(agent_dispatch, "cmd_list_applications", return_value={"applications": []}),
        mock.patch.object(
          agent_dispatch,
          "fetch_candidate_sources",
          return_value=([self.candidate(), second], {"jsearch": {"status": "ok", "rawCount": 2, "durationMs": 1}}),
        ),
        mock.patch.object(agent_dispatch, "register_channel_candidate_eligibility"),
      ):
        prepared = agent_dispatch.cmd_agent_prepare(self.args(root, "prepare"))
      claim_id = prepared["claims"][0]["claimId"]
      args = self.args(root, "claim", claim_id=claim_id)
      html = '<!doctype html><main class="cv" data-cv-template="professional-v2"><p data-cv-immutable="name">Test Applicant</p></main>'
      canonical = root / "canonical.html"
      canonical.write_text(html, encoding="utf-8")
      with mock.patch.object(agent_dispatch, "subscriber_cv_html", return_value=str(canonical)):
        claimed = agent_dispatch.cmd_agent_claim(args)
      work = dispatch_store.load_agent_work(root, "agent-test", claim_id)
      self.assertEqual(len(work["candidates"]), 2)
      artifact_dir = Path(claimed["work"]["personalizedOutputDirectory"]) / "job"
      artifact_dir.mkdir()
      cv = artifact_dir / "cv.html"
      cv.write_text(html, encoding="utf-8")
      letters = []
      for name in ("letter-1.txt", "letter-2.txt"):
        letter = artifact_dir / name
        letter.write_text("A truthful tailored letter.\n", encoding="utf-8")
        letters.append(letter)
      result = {
        "schemaVersion": 1,
        "runId": "agent-test",
        "claimId": claim_id,
        "assessments": [{
          "candidateId": candidate["agentCandidateId"],
          "fitScore": 84,
          "hardMismatch": False,
          "selected": True,
          "reason": "Strong fit.",
          "cvHtmlPath": str(cv),
          "coverLetterPath": str(letter_path),
        } for candidate, letter_path in zip(work["candidates"], letters)],
      }

      with self.assertRaisesRegex(agent_dispatch.AgentResultError, "its own artifact files"):
        agent_dispatch.validate_agent_result(self.args(root, "complete", claim_id=claim_id), work, result)

  def test_agent_phases_reject_dry_run(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      args = self.args(Path(tmp), "prepare")
      args.dry_run = True

      with self.assertRaisesRegex(SystemExit, "dry-run is not supported"):
        agent_dispatch.cmd_agent_dispatch(args)

  def test_unprocessed_channel_candidates_are_recorded_skipped(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      channel_candidate = agent_dispatch._agent_candidate({
        **self.candidate(),
        "sourceChannelJid": "123@newsletter",
        "sourcePostId": "post-1",
      })
      subscriber = self.subscriber()
      work = {
        "runId": "agent-test",
        "claimId": "claim-1",
        "userId": "user-1",
        "userKey": "user-1",
        "subscriber": subscriber,
        "preferences": subscriber["preferences"],
        "candidates": [channel_candidate],
        "applicationLimit": 1,
      }
      with (
        mock.patch.object(agent_dispatch, "cmd_list_applications", return_value={"applications": []}),
        mock.patch.object(agent_dispatch, "record_channel_candidate_outcome") as outcomes,
        mock.patch.object(agent_dispatch, "send_whatsapp_summary", return_value={"ok": True}),
        mock.patch.object(agent_dispatch, "publish_user_dashboard_telemetry"),
      ):
        outcome = agent_dispatch.dispatch_agent_user(self.args(root, "complete", claim_id="claim-1"), work, [], fallback=False)

      self.assertEqual(outcome["status"], "no_matches")
      outcomes.assert_called_once_with("agent-test", "user-1", mock.ANY, "skipped", 0)

  def test_dispatch_reports_personalization_source_counts(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      subscriber = self.subscriber()
      agent_candidate = agent_dispatch._agent_candidate({**self.candidate(), "role": "Driver", "company": "Alpha Freight"})
      template_candidate = agent_dispatch._agent_candidate({**self.candidate(), "role": "Courier", "company": "Beta Couriers"})
      work = {
        "runId": "agent-test",
        "claimId": "claim-1",
        "userId": "user-1",
        "subscriber": subscriber,
        "preferences": subscriber["preferences"],
        "candidates": [agent_candidate, template_candidate],
        "applicationLimit": 2,
      }
      # One application uses the agent's tailored CV + letter; the other has no
      # tailored artifacts, so it reports the deterministic template fallback.
      selected = [
        {"candidate": agent_candidate, "reason": "fit", "cvHtmlPath": root / "cv.html", "coverLetterPath": root / "letter.txt"},
        {"candidate": template_candidate, "reason": "fit", "cvHtmlPath": None, "coverLetterPath": None},
      ]
      template_letter = root / "template-letter.txt"
      template_letter.write_text("Dear Hiring Team,\n", encoding="utf-8")
      documents = {"cvPdf": root / "cv.pdf", "coverLetterPdf": root / "letter.pdf", "coverLetterText": template_letter}
      with (
        mock.patch.object(agent_dispatch, "cmd_list_applications", return_value={"applications": []}),
        mock.patch.object(agent_dispatch, "filter_candidates_for_user_with_stats", side_effect=lambda cands, *a, **k: (cands, {})),
        mock.patch.object(agent_dispatch, "candidate_attempt", return_value=(1, False)),
        mock.patch.object(agent_dispatch, "gmail_sender_connected", return_value=True),
        mock.patch.object(agent_dispatch, "write_cover_letter", return_value=template_letter),
        mock.patch.object(agent_dispatch, "_prepare_documents", return_value=documents),
        mock.patch.object(agent_dispatch, "send_application_email", return_value={"messageId": "m1"}),
        mock.patch.object(agent_dispatch, "record_candidate_application", return_value={}),
        mock.patch.object(agent_dispatch, "_stored_documents", return_value={}),
        mock.patch.object(agent_dispatch, "record_channel_candidate_outcome"),
        mock.patch.object(agent_dispatch, "send_whatsapp_summary", return_value={"ok": True}),
        mock.patch.object(agent_dispatch, "publish_user_dashboard_telemetry"),
      ):
        outcome = agent_dispatch.dispatch_agent_user(self.args(root, "complete", claim_id="claim-1"), work, selected, fallback=False)

      self.assertEqual(outcome["status"], "applied")
      self.assertEqual(outcome["appliedCount"], 2)
      self.assertEqual(outcome["personalizedCvCount"], 1)
      self.assertEqual(outcome["personalizedLetterCount"], 1)
      self.assertEqual(outcome["templateLetterCount"], 1)

  def test_inspector_captures_immutable_facts_across_void_elements(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "cv.html"
      path.write_text(
        "<img data-cv-field='identity' src='photo.jpg'>"
        "<p data-cv-field='contact'>+263 700 000<br>mail@example.com</p>"
        "<p data-cv-field='employer'>Acme</p>",
        encoding="utf-8",
      )
      inspector = agent_dispatch._inspect_html(path)

      self.assertEqual(inspector.immutable, [
        ("identity", ""),
        ("contact", "+263 700 000mail@example.com"),
        ("employer", "Acme"),
      ])

  def test_validator_treats_bare_and_self_closed_void_elements_alike(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      base = Path(tmp) / "base.html"
      faithful = Path(tmp) / "faithful.html"
      tampered = Path(tmp) / "tampered.html"
      base.write_text("<style>.cv{}</style><p data-cv-field='contact'>+263 700<br>a@b.c</p>", encoding="utf-8")
      faithful.write_text("<style>.cv{}</style><p data-cv-field='contact'>+263 700<br/>a@b.c</p>", encoding="utf-8")
      tampered.write_text("<style>.cv{}</style><p data-cv-field='contact'>+263 999<br>a@b.c</p>", encoding="utf-8")

      agent_dispatch._validate_personalized_html(base, faithful)
      with self.assertRaisesRegex(agent_dispatch.AgentResultError, "immutable fact"):
        agent_dispatch._validate_personalized_html(base, tampered)

  def test_stale_completing_claim_can_be_recovered(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      claim = dispatch_store.load_agent_claim(root, "agent-test", claim_id)
      claim["status"] = "completing"
      claim["completionStartedAt"] = agent_dispatch.utc_now()
      dispatch_store.save_agent_claim(root, "agent-test", claim_id, claim)
      fallback_args = self.args(root, "fallback", claim_id=claim_id)
      outcome = {
        "ok": True,
        "runId": "agent-test",
        "claimId": claim_id,
        "status": "no_matches",
        "appliedCount": 0,
        "actionRequiredCount": 0,
        "fallback": True,
        "fallbackReason": "child failed",
      }
      with self.assertRaisesRegex(SystemExit, "already in progress"):
        agent_dispatch.cmd_agent_fallback(fallback_args)

      claim["completionStartedAt"] = "2020-01-01T00:00:00Z"
      dispatch_store.save_agent_claim(root, "agent-test", claim_id, claim)
      with mock.patch.object(agent_dispatch, "dispatch_agent_user", return_value=outcome):
        result = agent_dispatch.cmd_agent_fallback(fallback_args)

      self.assertEqual(result["status"], "no_matches")
      recovered = dispatch_store.load_agent_claim(root, "agent-test", claim_id)
      self.assertIn(recovered["status"], agent_dispatch.TERMINAL_CLAIM_STATUSES)

  def test_system_exit_from_dispatch_records_failed_outcome(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      fallback_args = self.args(root, "fallback", claim_id=claim_id)
      with mock.patch.object(agent_dispatch, "dispatch_agent_user", side_effect=SystemExit("API request failed")):
        result = agent_dispatch.cmd_agent_fallback(fallback_args)

      self.assertEqual(result["status"], "failed")
      claim = dispatch_store.load_agent_claim(root, "agent-test", claim_id)
      self.assertIn(claim["status"], agent_dispatch.TERMINAL_CLAIM_STATUSES)
      with mock.patch.object(agent_dispatch, "finalize_channel_candidate_dispatch"):
        finished = agent_dispatch.cmd_agent_finish(self.args(root, "finish"))
      self.assertEqual(finished["finalCounts"]["processedCount"], 1)

  def test_orphaned_active_marker_starts_a_fresh_run(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      dispatch_store.set_active_agent_dispatch(root, {
        "runId": "ghost-run",
        "startedAt": agent_dispatch.utc_now(),
        "mode": "openclaw-agent",
      })
      prepared = self.prepare(root)

      self.assertFalse(prepared["resumed"])
      self.assertEqual(prepared["runId"], "agent-test")

  def test_finish_requires_all_claims_and_clears_active_marker(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      with self.assertRaisesRegex(SystemExit, "unfinished claim"):
        agent_dispatch.cmd_agent_finish(self.args(root, "finish"))
      work = dispatch_store.load_agent_work(root, "agent-test", claim_id)
      agent_dispatch._record_outcome(root, "agent-test", work, {
        "status": "no_matches",
        "appliedCount": 0,
        "actionRequiredCount": 0,
        "fallback": False,
      })
      with mock.patch.object(agent_dispatch, "finalize_channel_candidate_dispatch"):
        result = agent_dispatch.cmd_agent_finish(self.args(root, "finish"))

      self.assertEqual(result["finalCounts"]["processedCount"], 1)
      self.assertIsNone(dispatch_store.active_agent_dispatch(root))


if __name__ == "__main__":
  unittest.main()
