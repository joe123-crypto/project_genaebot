#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from jobscout import agent_dispatch, commands_dispatch, dispatch_store


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

  def test_preview_never_resumes_an_active_live_run(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      self.prepare(root)
      preview_args = self.args(root, "prepare", run_id="preview-run")
      preview_args.dry_run = True

      with self.assertRaisesRegex(SystemExit, "refusing to resume it as preview"):
        agent_dispatch.cmd_agent_prepare(preview_args)

  def test_claim_is_idempotent_and_materializes_canonical_html(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      args = self.args(root, "claim", claim_id=claim_id)
      # A current, unstyled semantic base CV (carries the template marker and no
      # inline stylesheet) is materialized verbatim.
      html = '<!doctype html><html><head></head><body><main class="cv" data-cv-template="professional-v2"><p data-cv-immutable="name">Test Applicant</p></main></body></html>'
      canonical = root / "canonical.html"
      canonical.write_text(html, encoding="utf-8")
      with mock.patch.object(agent_dispatch, "subscriber_cv_html", return_value=str(canonical)):
        first = agent_dispatch.cmd_agent_claim(args)
        second = agent_dispatch.cmd_agent_claim(args)

      base = Path(first["work"]["baseCvHtmlPath"])
      self.assertEqual(base.read_text(encoding="utf-8"), html)
      self.assertEqual(first["work"]["claimId"], second["work"]["claimId"])
      self.assertEqual(len(first["work"]["candidates"]), 1)
      self.assertEqual(first["work"]["candidates"][0]["assessmentNumber"], 1)
      self.assertIn("Never use write on resultPath", first["work"]["resultEditingGuidance"])

      result_path = Path(first["work"]["resultPath"])
      template = json.loads(result_path.read_text(encoding="utf-8"))
      expected_id = first["work"]["candidates"][0]["agentCandidateId"]
      self.assertEqual(template["assessments"], [{
        "assessmentNumber": 1,
        "candidateId": expected_id,
        "fitScore": "__FILL_FIT_SCORE_01__",
        "hardMismatch": "__FILL_HARD_MISMATCH_01__",
        "selected": "__FILL_SELECTED_01__",
        "reason": "__FILL_REASON_01__",
        "cvHtmlPath": None,
        "coverLetterPath": None,
      }])
      self.assertEqual(result_path.stat().st_mode & 0o777, 0o600)

      # An accidental idempotent claim cannot replace assessments already edited.
      template["assessments"][0]["reason"] = "Assessment work in progress"
      result_path.write_text(json.dumps(template), encoding="utf-8")
      with mock.patch.object(agent_dispatch, "subscriber_cv_html", return_value=str(canonical)):
        agent_dispatch.cmd_agent_claim(args)
      preserved = json.loads(result_path.read_text(encoding="utf-8"))
      self.assertEqual(preserved["assessments"][0]["reason"], "Assessment work in progress")

  def test_claim_rejects_unsafe_legacy_canonical_cv(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      args = self.args(root, "claim", claim_id=claim_id)
      canonical = root / "canonical.html"
      canonical.write_text(
        "<!doctype html><html><head><style>body { font-family: Georgia; }</style></head>"
        "<body><main>Old CV</main><p>file:///home/joseph/cv.html</p></body></html>",
        encoding="utf-8",
      )

      with mock.patch.object(agent_dispatch, "subscriber_cv_html", return_value=str(canonical)):
        with self.assertRaisesRegex(RuntimeError, "browser print chrome"):
          agent_dispatch.cmd_agent_claim(args)

  def test_validator_rejects_an_edited_prepopulated_candidate_id(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      canonical = root / "canonical.html"
      canonical.write_text(
        '<!doctype html><html><body><p data-cv-immutable="name">Test Applicant</p></body></html>',
        encoding="utf-8",
      )
      with mock.patch.object(agent_dispatch, "subscriber_cv_html", return_value=str(canonical)):
        claimed = agent_dispatch.cmd_agent_claim(self.args(root, "claim", claim_id=claim_id))

      result_path = Path(claimed["work"]["resultPath"])
      result = json.loads(result_path.read_text(encoding="utf-8"))
      assessment = result["assessments"][0]
      assessment.update({
        "fitScore": 10,
        "hardMismatch": True,
        "selected": False,
        "reason": "Required driving experience is missing.",
      })
      assessment["candidateId"] += "3"
      work = dispatch_store.load_agent_work(root, "agent-test", claim_id)

      with self.assertRaisesRegex(agent_dispatch.AgentResultError, "unknown or duplicate candidate"):
        agent_dispatch.validate_agent_result(
          self.args(root, "complete", claim_id=claim_id),
          work,
          result,
        )

  def test_validator_accepts_strong_fit_and_preserved_html(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      args = self.args(root, "claim", claim_id=claim_id)
      # Unstyled semantic base CV; the agent edits only mutable content.
      html = '<!doctype html><html><head></head><body><main class="cv" data-cv-template="professional-v2"><p data-cv-immutable="name">Test Applicant</p><p>Summary</p></main></body></html>'
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
      html = '<!doctype html><html><head></head><body><main class="cv" data-cv-template="professional-v2"><p data-cv-immutable="name">Test Applicant</p></main></body></html>'
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

      feed_headline = root / "feed-headline.txt"
      feed_headline.write_text(
        "Dear Hiring Team,\n\n"
        "I am applying for the catering opportunity at Midlands State University.\n"
        "My hospitality experience aligns with the waiter and porter duties,\n"
        "and I would welcome the chance to contribute to your team.\n\n"
        "Kind regards,\nTest Applicant\n",
        encoding="utf-8",
      )
      headline_candidate = {
        "role": "Midlands State University Catering Jobs July 2026: Waiters/Waitresses and Porter Vacancies",
        "company": "Unknown employer",
      }
      self.assertTrue(agent_dispatch._letter_is_tailored(feed_headline, headline_candidate))

  def test_weak_letter_fails_closed(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      args = self.args(root, "claim", claim_id=claim_id)
      html = '<!doctype html><html><head></head><body><main class="cv" data-cv-template="professional-v2"><p data-cv-immutable="name">Test Applicant</p><p>Summary</p></main></body></html>'
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

      with self.assertRaisesRegex(agent_dispatch.AgentResultError, "tailored-letter check"):
        agent_dispatch.validate_agent_result(self.args(root, "complete", claim_id=claim_id), work, result)

  def test_invalid_agent_result_fails_closed_and_duplicate_is_safe(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      claim_args = self.args(root, "claim", claim_id=claim_id)
      canonical = root / "canonical.html"
      canonical.write_text("<html><head><style></style></head><body><p>Test Applicant</p></body></html>", encoding="utf-8")
      with mock.patch.object(agent_dispatch, "subscriber_cv_html", return_value=str(canonical)):
        agent_dispatch.cmd_agent_claim(claim_args)
      complete_args = self.args(root, "complete", claim_id=claim_id)
      complete_args.result = json.dumps({"schemaVersion": 1, "runId": "wrong", "claimId": claim_id, "assessments": []})
      with mock.patch.object(agent_dispatch, "dispatch_agent_user") as dispatch:
        first = agent_dispatch.cmd_agent_complete(complete_args)
        second = agent_dispatch.cmd_agent_complete(complete_args)

      self.assertEqual(first["status"], "failed")
      self.assertFalse(first["fallback"])
      self.assertIn("does not belong to this claim", first["failureReason"])
      self.assertTrue(second["duplicate"])
      dispatch.assert_not_called()

  def test_durable_outcome_restores_a_tampered_terminal_claim(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      complete_args = self.args(root, "complete", claim_id=claim_id)
      complete_args.result = json.dumps({
        "schemaVersion": 1,
        "runId": "wrong",
        "claimId": claim_id,
        "assessments": [],
      })
      first = agent_dispatch.cmd_agent_complete(complete_args)
      self.assertEqual(first["status"], "failed")

      # Simulate a model directly editing the claim after its first terminal
      # outcome. The immutable dispatch outcome must restore terminal failure.
      claim = dispatch_store.load_agent_claim(root, "agent-test", claim_id)
      claim["status"] = "claimed"
      claim.pop("outcome", None)
      claim.pop("completionStartedAt", None)
      dispatch_store.save_agent_claim(root, "agent-test", claim_id, claim)

      duplicate = agent_dispatch.cmd_agent_complete(complete_args)
      restored = dispatch_store.load_agent_claim(root, "agent-test", claim_id)
      marker = dispatch_store.load_dispatch("agent-test", root)

      self.assertTrue(duplicate["duplicate"])
      self.assertEqual(duplicate["status"], "failed")
      self.assertEqual(restored["status"], "failed")
      self.assertEqual(restored["outcome"]["status"], "failed")
      self.assertEqual(marker["outcomes"][restored["userKey"]]["status"], "failed")

  def test_dispatch_outcome_cannot_be_overwritten_or_added_after_finish(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      self.prepare(root)
      record = argparse.Namespace(
        run_id="agent-test",
        user_id="user-1",
        status="failed",
        applied_count=0,
        action_required_count=0,
        dispatch_root=root,
      )
      first = commands_dispatch.cmd_record_dispatch_outcome(record)
      record.status = "preview"
      duplicate = commands_dispatch.cmd_record_dispatch_outcome(record)
      marker = dispatch_store.load_dispatch("agent-test", root)

      self.assertFalse(first["duplicate"])
      self.assertTrue(duplicate["duplicate"])
      self.assertEqual(marker["outcomes"][agent_dispatch.user_key("user-1")]["status"], "failed")

      marker["finishedAt"] = agent_dispatch.utc_now()
      dispatch_store.save_dispatch(marker, root)
      record.user_id = "user-2"
      with self.assertRaisesRegex(SystemExit, "already finished"):
        commands_dispatch.cmd_record_dispatch_outcome(record)

  def test_validator_rejects_oversized_personalized_html(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      args = self.args(root, "claim", claim_id=claim_id)
      html = '<!doctype html><html><head></head><body><main class="cv" data-cv-template="professional-v2"><p data-cv-immutable="name">Test Applicant</p></main></body></html>'
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
      html = '<!doctype html><html><head></head><body><main class="cv" data-cv-template="professional-v2"><p data-cv-immutable="name">Test Applicant</p></main></body></html>'
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
      for name, company, role in (
        ("letter-1.txt", "Example Logistics", "Driver"),
        ("letter-2.txt", "Other Logistics", "Driver Assistant"),
      ):
        letter = artifact_dir / name
        letter.write_text(
          f"Dear Hiring Team at {company},\n\nI am applying for the {role} role at {company}.\n"
          "My safe delivery experience fits this work, and I can contribute reliably.\n\n"
          "Kind regards,\nTest Applicant\n",
          encoding="utf-8",
        )
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

  def test_agent_prepare_supports_preview_without_side_effectful_preparation(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      args = self.args(Path(tmp), "prepare")
      args.dry_run = True

      with (
        mock.patch.object(agent_dispatch, "cmd_list_ready", return_value={"subscribers": []}),
        mock.patch.object(agent_dispatch, "convert_pending_cvs_best_effort") as convert,
        mock.patch.object(agent_dispatch, "maybe_write_group_feed_cache") as feed,
      ):
        result = agent_dispatch.cmd_agent_dispatch(args)

      self.assertTrue(result["dryRun"])
      convert.assert_not_called()
      feed.assert_not_called()

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
      work = {
        "runId": "agent-test",
        "claimId": "claim-1",
        "userId": "user-1",
        "subscriber": subscriber,
        "preferences": subscriber["preferences"],
        "candidates": [agent_candidate],
        "applicationLimit": 1,
      }
      selected = [
        {"candidate": agent_candidate, "reason": "fit", "cvHtmlPath": root / "cv.html", "coverLetterPath": root / "letter.txt"},
      ]
      template_letter = root / "template-letter.txt"
      template_letter.write_text("Dear Hiring Team,\n", encoding="utf-8")
      documents = {"cvPdf": root / "cv.pdf", "coverLetterPdf": root / "letter.pdf", "coverLetterText": template_letter}
      with (
        mock.patch.object(agent_dispatch, "cmd_list_applications", return_value={"applications": []}),
        mock.patch.object(agent_dispatch, "filter_candidates_for_user_with_stats", side_effect=lambda cands, *a, **k: (cands, {})),
        mock.patch.object(agent_dispatch, "candidate_attempt", return_value=(1, False)),
        mock.patch.object(agent_dispatch, "gmail_sender_connected", return_value=True),
        mock.patch.object(agent_dispatch, "_prepare_documents", return_value=documents),
        mock.patch.object(agent_dispatch, "send_application_email", return_value={"messageId": "m1"}),
        mock.patch.object(agent_dispatch, "record_candidate_application", return_value={}),
        mock.patch.object(agent_dispatch, "_stored_documents", return_value={}),
        mock.patch.object(agent_dispatch, "record_channel_candidate_outcome"),
        mock.patch.object(agent_dispatch, "send_whatsapp_summary", side_effect=RuntimeError("gateway unavailable")),
        mock.patch.object(agent_dispatch, "publish_user_dashboard_telemetry"),
      ):
        outcome = agent_dispatch.dispatch_agent_user(self.args(root, "complete", claim_id="claim-1"), work, selected, fallback=False)

      self.assertEqual(outcome["status"], "applied")
      self.assertEqual(outcome["appliedCount"], 1)
      self.assertEqual(outcome["personalizedCvCount"], 1)
      self.assertEqual(outcome["personalizedLetterCount"], 1)
      self.assertEqual(outcome["templateLetterCount"], 0)
      self.assertFalse(outcome["summaryDelivered"])
      self.assertEqual(outcome["summaryError"], "gateway unavailable")

  def test_dispatch_rejects_missing_personalized_artifacts(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      subscriber = self.subscriber()
      candidate = agent_dispatch._agent_candidate(self.candidate())
      work = {
        "runId": "agent-test",
        "claimId": "claim-1",
        "userId": "user-1",
        "subscriber": subscriber,
        "preferences": subscriber["preferences"],
        "candidates": [candidate],
        "applicationLimit": 1,
      }
      selected = [{"candidate": candidate, "reason": "fit", "cvHtmlPath": None, "coverLetterPath": None}]
      with (
        mock.patch.object(agent_dispatch, "cmd_list_applications", return_value={"applications": []}),
        mock.patch.object(agent_dispatch, "filter_candidates_for_user_with_stats", side_effect=lambda cands, *a, **k: (cands, {})),
        mock.patch.object(agent_dispatch, "candidate_attempt", return_value=(1, False)),
      ):
        with self.assertRaisesRegex(RuntimeError, "personalized CV and cover letter"):
          agent_dispatch.dispatch_agent_user(self.args(root, "complete", claim_id="claim-1"), work, selected, fallback=False)

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

  def test_claim_packet_includes_cv_design_guidance(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      html = '<!doctype html><html><head></head><body><main class="cv" data-cv-template="professional-v2"><p data-cv-immutable="name">Test Applicant</p></main></body></html>'
      canonical = root / "canonical.html"
      canonical.write_text(html, encoding="utf-8")
      with mock.patch.object(agent_dispatch, "subscriber_cv_html", return_value=str(canonical)):
        claimed = agent_dispatch.cmd_agent_claim(self.args(root, "claim", claim_id=claim_id))
      guidance = claimed["work"]["cvDesignGuidance"]
      self.assertIn("single A4 page", guidance)
      self.assertIn("self-contained", guidance)
      self.assertIn("never by dropping facts", guidance.lower())

  def test_validator_accepts_full_redesign_and_guards_facts(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      base = root / "base.html"
      base.write_text(
        '<main data-cv-template="professional-v2"><h1 data-cv-field="identity">Joseph Munemo</h1>'
        '<p data-cv-field="contact">+263 78 563 2943<br>me@example.com</p>'
        '<p data-cv-field="employer">Acme 2019 - 2024</p></main>',
        encoding="utf-8",
      )
      # A completely different design + CSS that keeps every fact: accepted.
      good = root / "good.html"
      good.write_text(
        '<html><head><style>*{font-family:Georgia}</style></head><body>'
        '<header><h1>Joseph Munemo</h1></header>'
        '<aside>Call +263 78 563 2943 or email me@example.com</aside>'
        '<div>Acme, 2019 to 2024</div></body></html>',
        encoding="utf-8",
      )
      agent_dispatch._validate_personalized_html(base, good)

      def variant(name: str, body: str) -> Path:
        path = root / name
        path.write_text(f"<html><body>{body}</body></html>", encoding="utf-8")
        return path

      keep = "Joseph Munemo +263 78 563 2943 me@example.com Acme 2019 to 2024"
      with self.assertRaisesRegex(agent_dispatch.AgentResultError, "email"):
        agent_dispatch._validate_personalized_html(base, variant("drop_email.html", keep.replace("me@example.com", "")))
      with self.assertRaisesRegex(agent_dispatch.AgentResultError, "phone number not in"):
        agent_dispatch._validate_personalized_html(base, variant("add_phone.html", keep + " +263 71 111 2222"))
      with self.assertRaisesRegex(agent_dispatch.AgentResultError, "date"):
        agent_dispatch._validate_personalized_html(base, variant("drop_date.html", keep.replace(" to 2024", "")))
      with self.assertRaisesRegex(agent_dispatch.AgentResultError, "name"):
        agent_dispatch._validate_personalized_html(base, variant("rename.html", keep.replace("Joseph Munemo", "Someone Else")))

  def test_validator_rejects_non_self_contained_html(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      base = root / "base.html"
      base.write_text('<h1 data-cv-field="identity">Jane Doe</h1><p>me@example.com</p>', encoding="utf-8")
      for name, body in [
        ("script.html", "<script>x()</script>Jane Doe me@example.com"),
        ("img.html", "<img src='https://tracker.example/p.gif'>Jane Doe me@example.com"),
        ("link.html", "<link rel='stylesheet' href='https://cdn.example/x.css'>Jane Doe me@example.com"),
        ("import.html", "<style>@import url(https://cdn.example/x.css);</style>Jane Doe me@example.com"),
      ]:
        path = root / name
        path.write_text(f"<html><head></head><body>{body}</body></html>", encoding="utf-8")
        with self.assertRaisesRegex(agent_dispatch.AgentResultError, "unsafe or non-self-contained"):
          agent_dispatch._validate_personalized_html(base, path)

  def test_validator_preserves_facts_across_void_elements_and_restyling(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      base = Path(tmp) / "base.html"
      faithful = Path(tmp) / "faithful.html"
      tampered = Path(tmp) / "tampered.html"
      base.write_text("<style>.cv{}</style><p data-cv-field='contact'>+263 771 234<br>a@example.com</p>", encoding="utf-8")
      # A full redesign (different markup/CSS, <br> vs <br/>) is fine as long as
      # the facts survive.
      faithful.write_text("<html><head><style>.new{color:red}</style></head><body><section><b>+263 771 234</b><br/> a@example.com</section></body></html>", encoding="utf-8")
      tampered.write_text("<html><head><style>.cv{}</style></head><body><p>+263 999 999<br>a@example.com</p></body></html>", encoding="utf-8")

      agent_dispatch._validate_personalized_html(base, faithful)
      with self.assertRaisesRegex(agent_dispatch.AgentResultError, "phone number"):
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
      with self.assertRaisesRegex(SystemExit, "already in progress"):
        agent_dispatch.cmd_agent_fallback(fallback_args)

      claim["completionStartedAt"] = "2020-01-01T00:00:00Z"
      dispatch_store.save_agent_claim(root, "agent-test", claim_id, claim)
      result = agent_dispatch.cmd_agent_fallback(fallback_args)

      self.assertEqual(result["status"], "failed")
      self.assertFalse(result["fallback"])
      recovered = dispatch_store.load_agent_claim(root, "agent-test", claim_id)
      self.assertIn(recovered["status"], agent_dispatch.TERMINAL_CLAIM_STATUSES)

  def test_system_exit_from_dispatch_records_failed_outcome(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      fallback_args = self.args(root, "fallback", claim_id=claim_id)
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
