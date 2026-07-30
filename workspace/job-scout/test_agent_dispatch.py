#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from jobscout import agent_dispatch, cli, commands_dispatch, dispatch_store


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

  def low_information_candidate(self) -> dict:
    return {
      **self.candidate(),
      "candidateId": "low-info",
      "sourceUrl": None,
      "applyLinks": [],
      "applicationEmail": None,
      "applicationEmails": [],
      "company": "Unknown employer",
      "location": "",
      "closing": None,
      "description": "Driver Wanted",
    }

  def missing_title_candidate(self) -> dict:
    return {
      **self.candidate(),
      "candidateId": "missing-title",
      "role": "just a paycheck. We spend nearly a third of our lives at work.",
      "company": "Unknown employer",
      "location": "",
      "description": (
        "Thank you to everyone who participated in my survey. I genuinely appreciate every single vote. "
        "The topic was workplace wellbeing and employee betterment programmes."
      ),
    }

  def args(self, root: Path, phase: str, run_id: str | None = "agent-test", claim_id: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
      agent_phase=phase,
      run_id=run_id,
      claim_id=claim_id,
      result=None,
      fallback_reason=None,
      after_terminal_count=None,
      wait_timeout_seconds=2760,
      phone=None,
      user_id=None,
      limit_ready=100,
      max_candidates_per_user=5,
      shortlist_limit=20,
      match_threshold=51,
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

  def test_approved_prepare_rebuilds_candidate_from_stored_snapshot_without_discovery(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      args = self.args(root, "prepare", run_id="approved-test")
      args.approved_only = True
      approved = {
        "applicationId": "application-1",
        "status": "approved",
        "company": "Example Logistics",
        "role": "Driver",
        "applicationEmail": "jobs@example.com",
        "matchReason": "Strong driving and location fit.",
        "submissionMethod": "email",
        "candidateSnapshot": self.candidate(),
      }
      with (
        mock.patch.object(agent_dispatch, "cmd_list_ready", return_value={"subscribers": [self.subscriber()]}),
        mock.patch.object(agent_dispatch, "cmd_list_applications", return_value={"applications": [approved]}),
        mock.patch.object(agent_dispatch, "fetch_candidate_sources") as discover,
        mock.patch.object(agent_dispatch, "convert_pending_cvs_best_effort") as convert,
      ):
        prepared = agent_dispatch.cmd_agent_prepare(args)

      self.assertEqual(prepared["claimCount"], 1)
      claim_id = prepared["claims"][0]["claimId"]
      work = dispatch_store.load_agent_work(root, "approved-test", claim_id)
      self.assertTrue(work["approvedSubmission"])
      self.assertFalse(work["approvalRequired"])
      self.assertEqual(work["applicationLimit"], 1)
      self.assertEqual(work["candidates"][0]["applicationEmail"], "jobs@example.com")
      self.assertEqual(work["candidates"][0]["approvalApplicationId"], "application-1")
      discover.assert_not_called()
      convert.assert_not_called()

  def test_manual_policy_validator_accepts_selected_match_with_null_artifacts(self) -> None:
    candidate = agent_dispatch._agent_candidate(self.candidate())
    work = {
      "runId": "agent-test",
      "claimId": "claim-1",
      "subscriber": self.subscriber(),
      "preferences": {**self.subscriber()["preferences"], "autoApply": False},
      "candidates": [candidate],
      "applicationLimit": 1,
      "matchThreshold": 65,
      "approvalRequired": True,
    }
    result = {
      "schemaVersion": 1,
      "runId": "agent-test",
      "claimId": "claim-1",
      "assessments": [{
        "assessmentNumber": 1,
        "candidateId": candidate["agentCandidateId"],
        "fitScore": 82,
        "hardMismatch": False,
        "selected": True,
        "reason": "The driving experience and Harare location align.",
        "cvHtmlPath": None,
        "coverLetterPath": None,
      }],
    }

    selected = agent_dispatch.validate_agent_result(
      self.args(Path("/tmp"), "complete", claim_id="claim-1"),
      work,
      result,
    )

    self.assertEqual(len(selected), 1)
    self.assertIsNone(selected[0]["cvHtmlPath"])
    self.assertIsNone(selected[0]["coverLetterPath"])

  def test_default_match_threshold_accepts_51_but_not_50(self) -> None:
    parsed = cli.build_parser().parse_args(["dispatch-jsearch", "--agent-phase", "status"])
    self.assertEqual(parsed.match_threshold, 51)
    candidate = agent_dispatch._agent_candidate(self.candidate())
    work = {
      "runId": "agent-test",
      "claimId": "claim-1",
      "subscriber": self.subscriber(),
      "preferences": {**self.subscriber()["preferences"], "autoApply": False},
      "candidates": [candidate],
      "applicationLimit": 1,
      "approvalRequired": True,
    }

    def result(score: int, selected: bool) -> dict:
      return {
        "schemaVersion": 1,
        "runId": "agent-test",
        "claimId": "claim-1",
        "assessments": [{
          "assessmentNumber": 1,
          "candidateId": candidate["agentCandidateId"],
          "fitScore": score,
          "hardMismatch": False,
          "selected": selected,
          "reason": "The role and location have a plausible fit.",
          "cvHtmlPath": None,
          "coverLetterPath": None,
        }],
      }

    rejected = agent_dispatch.validate_agent_result(
      self.args(Path("/tmp"), "complete", claim_id="claim-1"),
      work,
      result(50, False),
    )
    accepted = agent_dispatch.validate_agent_result(
      self.args(Path("/tmp"), "complete", claim_id="claim-1"),
      work,
      result(51, True),
    )
    self.assertEqual(rejected, [])
    self.assertEqual(len(accepted), 1)

  def test_validator_requires_top_eligible_match_with_ties_in_shortlist_order(self) -> None:
    candidates = [
      agent_dispatch._agent_candidate({
        **self.candidate(),
        "candidateId": f"source-job-{index}",
        "role": f"Driver {index}",
        "sourceUrl": f"https://jobs.example/driver-{index}",
        "applyLinks": [f"https://jobs.example/driver-{index}"],
      })
      for index in range(1, 4)
    ]
    work = {
      "runId": "agent-test",
      "claimId": "claim-1",
      "subscriber": self.subscriber(),
      "preferences": {**self.subscriber()["preferences"], "autoApply": False},
      "candidates": candidates,
      "applicationLimit": 1,
      "matchThreshold": 51,
      "approvalRequired": True,
    }

    def result(selected_number: int) -> dict:
      return {
        "schemaVersion": 1,
        "runId": "agent-test",
        "claimId": "claim-1",
        "assessments": [
          {
            "assessmentNumber": index,
            "candidateId": candidate["agentCandidateId"],
            "fitScore": score,
            "hardMismatch": hard_mismatch,
            "selected": index == selected_number,
            "reason": "Evidence-based assessment.",
            "cvHtmlPath": None,
            "coverLetterPath": None,
          }
          for index, (candidate, score, hard_mismatch) in enumerate(
            zip(candidates, (90, 60, 60), (True, False, False)),
            1,
          )
        ],
      }

    selected = agent_dispatch.validate_agent_result(
      self.args(Path("/tmp"), "complete", claim_id="claim-1"),
      work,
      result(2),
    )
    self.assertEqual(selected[0]["candidate"], candidates[1])
    with self.assertRaisesRegex(agent_dispatch.AgentResultError, "highest-scoring eligible"):
      agent_dispatch.validate_agent_result(
        self.args(Path("/tmp"), "complete", claim_id="claim-1"),
        work,
        result(3),
      )

  def test_prepare_excludes_low_information_candidates_from_work_packet(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      with (
        mock.patch.object(agent_dispatch, "cmd_list_ready", return_value={"subscribers": [self.subscriber()]}),
        mock.patch.object(agent_dispatch, "cmd_list_applications", return_value={"applications": []}),
        mock.patch.object(
          agent_dispatch,
          "fetch_candidate_sources",
          return_value=([self.low_information_candidate()], {
            "jsearch": {"status": "ok", "rawCount": 1, "durationMs": 1},
          }),
        ),
        mock.patch.object(agent_dispatch, "register_channel_candidate_eligibility") as register,
      ):
        prepared = agent_dispatch.cmd_agent_prepare(self.args(root, "prepare"))

      claim_id = prepared["claims"][0]["claimId"]
      work = dispatch_store.load_agent_work(root, "agent-test", claim_id)
      self.assertEqual(prepared["claims"][0]["candidateCount"], 0)
      self.assertEqual(work["candidates"], [])
      self.assertEqual(work["globalFilter"]["jsearch"]["lowInfoCount"], 1)
      register.assert_not_called()

  def test_prepare_excludes_missing_title_candidates_from_work_packet(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      with (
        mock.patch.object(agent_dispatch, "cmd_list_ready", return_value={"subscribers": [self.subscriber()]}),
        mock.patch.object(agent_dispatch, "cmd_list_applications", return_value={"applications": []}),
        mock.patch.object(
          agent_dispatch,
          "fetch_candidate_sources",
          return_value=([self.missing_title_candidate()], {
            "jsearch": {"status": "ok", "rawCount": 1, "durationMs": 1},
          }),
        ),
        mock.patch.object(agent_dispatch, "register_channel_candidate_eligibility") as register,
      ):
        prepared = agent_dispatch.cmd_agent_prepare(self.args(root, "prepare"))

      claim_id = prepared["claims"][0]["claimId"]
      work = dispatch_store.load_agent_work(root, "agent-test", claim_id)
      self.assertEqual(prepared["claims"][0]["candidateCount"], 0)
      self.assertEqual(work["candidates"], [])
      self.assertEqual(work["globalFilter"]["jsearch"]["missingTitleCount"], 1)
      register.assert_not_called()

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

  def test_validator_maps_numbered_assessment_when_candidate_id_is_edited(self) -> None:
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

      self.assertEqual(
        agent_dispatch.validate_agent_result(
          self.args(root, "complete", claim_id=claim_id),
          work,
          result,
        ),
        [],
      )

      assessment["assessmentNumber"] = 2
      with self.assertRaisesRegex(agent_dispatch.AgentResultError, "assessmentNumber values"):
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

  def test_validate_is_side_effect_free_and_reports_selected_count(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      before_claim = dispatch_store.load_agent_claim(root, "agent-test", claim_id)
      before_marker = dispatch_store.load_dispatch("agent-test", root)
      validate_args = self.args(root, "validate", claim_id=claim_id)
      validate_args.result = json.dumps({
        "schemaVersion": 1,
        "runId": "agent-test",
        "claimId": claim_id,
        "assessments": [{
          "assessmentNumber": 1,
          "candidateId": "pre-populated-id-is-not-authoritative",
          "fitScore": 10,
          "hardMismatch": True,
          "selected": False,
          "reason": "Required driving experience is missing.",
          "cvHtmlPath": None,
          "coverLetterPath": None,
        }],
      })

      with mock.patch.object(agent_dispatch, "_begin_completion") as begin:
        result = agent_dispatch.cmd_agent_validate(validate_args)

      self.assertEqual(result["selectedCount"], 0)
      begin.assert_not_called()
      self.assertEqual(dispatch_store.load_agent_claim(root, "agent-test", claim_id), before_claim)
      self.assertEqual(dispatch_store.load_dispatch("agent-test", root), before_marker)

  def test_validate_rejects_a_personalized_cv_with_an_invented_phone(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      canonical = root / "canonical.html"
      canonical.write_text(
        "<html><body><p data-cv-field='identity'>Test Applicant</p>"
        "<p data-cv-field='contact'>+263 700 000001 applicant@example.com</p></body></html>",
        encoding="utf-8",
      )
      with mock.patch.object(agent_dispatch, "subscriber_cv_html", return_value=str(canonical)):
        claimed = agent_dispatch.cmd_agent_claim(self.args(root, "claim", claim_id=claim_id))
      output = Path(claimed["work"]["personalizedOutputDirectory"]) / "job"
      output.mkdir()
      cv = output / "cv.html"
      cv.write_text(
        "<html><body>Test Applicant +263 700 000001 applicant@example.com "
        "+263 711 111111</body></html>",
        encoding="utf-8",
      )
      letter = output / "cover-letter.txt"
      letter.write_text(
        "Dear Hiring Team at Example Logistics,\n\nI am applying for the Driver position.\n"
        "My safe parcel delivery experience suits this role and I can contribute reliably.\n\n"
        "Kind regards,\nTest Applicant\n",
        encoding="utf-8",
      )
      validate_args = self.args(root, "validate", claim_id=claim_id)
      validate_args.result = json.dumps({
        "schemaVersion": 1,
        "runId": "agent-test",
        "claimId": claim_id,
        "assessments": [{
          "assessmentNumber": 1,
          "candidateId": claimed["work"]["candidates"][0]["agentCandidateId"],
          "fitScore": 90,
          "hardMismatch": False,
          "selected": True,
          "reason": "Strong fit.",
          "cvHtmlPath": str(cv),
          "coverLetterPath": str(letter),
        }],
      })

      with self.assertRaisesRegex(agent_dispatch.AgentResultError, "phone number not in"):
        agent_dispatch.cmd_agent_validate(validate_args)

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

  def test_web_only_subscriber_sends_email_and_skips_whatsapp_summary(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      subscriber = {
        **self.subscriber(),
        "whatsappPhone": None,
        "whatsappPhoneHash": None,
      }
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
      selected = [{
        "candidate": candidate,
        "reason": "strong fit",
        "cvHtmlPath": root / "cv.html",
        "coverLetterPath": root / "letter.txt",
      }]
      documents = {
        "cvPdf": root / "cv.pdf",
        "coverLetterPdf": root / "letter.pdf",
        "coverLetterText": root / "letter.txt",
      }
      with (
        mock.patch.object(agent_dispatch, "cmd_list_applications", return_value={"applications": []}),
        mock.patch.object(agent_dispatch, "filter_candidates_for_user_with_stats", side_effect=lambda cands, *a, **k: (cands, {})),
        mock.patch.object(agent_dispatch, "gmail_sender_connected", return_value=True),
        mock.patch.object(agent_dispatch, "_prepare_documents", return_value=documents),
        mock.patch.object(agent_dispatch, "send_application_email", return_value={"messageId": "gmail-1"}) as send,
        mock.patch.object(agent_dispatch, "record_candidate_application", return_value={}) as record,
        mock.patch.object(agent_dispatch, "_stored_documents", return_value={}),
        mock.patch.object(agent_dispatch, "record_channel_candidate_outcome"),
        mock.patch.object(agent_dispatch, "send_whatsapp_summary") as whatsapp,
        mock.patch.object(agent_dispatch, "publish_user_dashboard_telemetry") as telemetry,
      ):
        outcome = agent_dispatch.dispatch_agent_user(
          self.args(root, "complete", claim_id="claim-1"),
          work,
          selected,
          fallback=False,
        )

      self.assertEqual(outcome["status"], "applied")
      self.assertEqual(outcome["appliedCount"], 1)
      self.assertIsNone(outcome["summaryDelivered"])
      self.assertIsNone(outcome["summaryError"])
      send.assert_called_once()
      whatsapp.assert_not_called()
      self.assertEqual(record.call_args.kwargs["message_id"], "gmail-1")
      self.assertIsNone(telemetry.call_args.args[4])

  def test_dispatch_rechecks_weekday_prefixed_expiry_before_sending(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      subscriber = self.subscriber()
      candidate = agent_dispatch._agent_candidate({
        **self.candidate(),
        "closing": "Wednesday, 29 July 2026",
      })
      work = {
        "runId": "agent-test",
        "claimId": "claim-1",
        "userId": "user-1",
        "subscriber": subscriber,
        "preferences": subscriber["preferences"],
        "candidates": [candidate],
        "applicationLimit": 1,
      }
      selected = [{
        "candidate": candidate,
        "reason": "The role and location align.",
        "cvHtmlPath": root / "cv.html",
        "coverLetterPath": root / "letter.txt",
      }]
      with (
        mock.patch.object(agent_dispatch, "cmd_list_applications", return_value={"applications": []}),
        mock.patch.object(agent_dispatch, "record_candidate_application") as record,
        mock.patch.object(agent_dispatch, "_prepare_documents") as prepare_documents,
        mock.patch.object(agent_dispatch, "send_application_email") as send,
        mock.patch.object(agent_dispatch, "request_web_application") as web_apply,
        mock.patch.object(agent_dispatch, "record_channel_candidate_outcome"),
        mock.patch.object(agent_dispatch, "send_whatsapp_summary", return_value={"ok": True}),
        mock.patch.object(agent_dispatch, "publish_user_dashboard_telemetry"),
      ):
        outcome = agent_dispatch.dispatch_agent_user(
          self.args(root, "complete", claim_id="claim-1"),
          work,
          selected,
          fallback=False,
        )

      self.assertEqual(outcome["status"], "no_matches")
      self.assertEqual(outcome["appliedCount"], 0)
      record.assert_not_called()
      prepare_documents.assert_not_called()
      send.assert_not_called()
      web_apply.assert_not_called()

  def test_manual_policy_records_email_match_without_personalizing_or_sending(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      subscriber = self.subscriber()
      subscriber["preferences"]["autoApply"] = False
      email_candidate = agent_dispatch._agent_candidate(self.candidate())
      website_candidate = agent_dispatch._agent_candidate({
        **self.candidate(),
        "candidateId": "source-job-2",
        "role": "Warehouse Assistant",
        "applicationEmail": None,
        "applicationEmails": [],
      })
      work = {
        "runId": "agent-test",
        "claimId": "claim-1",
        "userId": "user-1",
        "subscriber": subscriber,
        "preferences": subscriber["preferences"],
        "candidates": [email_candidate, website_candidate],
        "applicationLimit": 2,
        "approvalRequired": True,
      }
      selected = [
        {
          "candidate": email_candidate,
          "reason": "Strong driving and location fit.",
          "cvHtmlPath": None,
          "coverLetterPath": None,
        },
        {
          "candidate": website_candidate,
          "reason": "Strong logistics and location fit.",
          "cvHtmlPath": None,
          "coverLetterPath": None,
        },
      ]
      with (
        mock.patch.object(agent_dispatch, "cmd_list_applications", return_value={"applications": []}),
        mock.patch.object(agent_dispatch, "filter_candidates_for_user_with_stats", side_effect=lambda cands, *a, **k: (cands, {})),
        mock.patch.object(agent_dispatch, "record_candidate_application", return_value={}) as record,
        mock.patch.object(agent_dispatch, "_prepare_documents") as prepare_documents,
        mock.patch.object(agent_dispatch, "send_application_email") as send,
        mock.patch.object(agent_dispatch, "request_web_application") as web_apply,
        mock.patch.object(agent_dispatch, "record_channel_candidate_outcome"),
        mock.patch.object(agent_dispatch, "send_whatsapp_summary", return_value={"ok": True}),
        mock.patch.object(agent_dispatch, "publish_user_dashboard_telemetry"),
      ):
        outcome = agent_dispatch.dispatch_agent_user(
          self.args(root, "complete", claim_id="claim-1"),
          work,
          selected,
          fallback=False,
        )

      self.assertEqual(outcome["status"], "pending_approval")
      self.assertEqual(outcome["pendingApprovalCount"], 2)
      self.assertEqual([call.args[2] for call in record.call_args_list], ["pending_approval", "pending_approval"])
      self.assertEqual([call.kwargs["submission_method"] for call in record.call_args_list], ["email", "website"])
      prepare_documents.assert_not_called()
      send.assert_not_called()
      web_apply.assert_not_called()

  def test_approved_email_application_is_personalized_and_sent_with_auto_apply_off(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      subscriber = self.subscriber()
      subscriber["preferences"]["autoApply"] = False
      candidate = agent_dispatch._agent_candidate({
        **self.candidate(),
        "approvalApplicationId": "application-1",
        "approvalMatchReason": "Previously selected as a strong fit.",
        "approvalSubmissionMethod": "email",
      })
      work = {
        "runId": "approved-test",
        "claimId": "claim-1",
        "userId": "user-1",
        "subscriber": subscriber,
        "preferences": subscriber["preferences"],
        "candidates": [candidate],
        "applicationLimit": 1,
        "approvedSubmission": True,
      }
      selected = [{
        "candidate": candidate,
        "reason": "Previously selected as a strong fit.",
        "cvHtmlPath": root / "cv.html",
        "coverLetterPath": root / "letter.txt",
      }]
      documents = {
        "cvPdf": root / "cv.pdf",
        "coverLetterPdf": root / "letter.pdf",
        "coverLetterText": root / "letter.txt",
      }
      existing = {**candidate, "status": "approved", "attemptCount": 0}
      with (
        mock.patch.object(agent_dispatch, "cmd_list_applications", return_value={"applications": [existing]}),
        mock.patch.object(agent_dispatch, "gmail_sender_connected", return_value=True),
        mock.patch.object(agent_dispatch, "_prepare_documents", return_value=documents) as prepare_documents,
        mock.patch.object(agent_dispatch, "send_application_email", return_value={"messageId": "gmail-approved"}) as send,
        mock.patch.object(agent_dispatch, "record_candidate_application", return_value={}) as record,
        mock.patch.object(agent_dispatch, "_stored_documents", return_value={}),
        mock.patch.object(agent_dispatch, "record_channel_candidate_outcome"),
        mock.patch.object(agent_dispatch, "send_whatsapp_summary", return_value={"ok": True}),
        mock.patch.object(agent_dispatch, "publish_user_dashboard_telemetry"),
      ):
        outcome = agent_dispatch.dispatch_agent_user(
          self.args(root, "complete", claim_id="claim-1"),
          work,
          selected,
          fallback=False,
        )

      self.assertEqual(outcome["status"], "applied")
      prepare_documents.assert_called_once()
      send.assert_called_once()
      self.assertEqual(record.call_args.args[2], "applied")
      self.assertTrue(record.call_args.kwargs["replace"])

  def test_approved_application_that_expired_is_skipped_without_sending(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      subscriber = self.subscriber()
      subscriber["preferences"]["autoApply"] = False
      candidate = agent_dispatch._agent_candidate({
        **self.candidate(),
        "approvalApplicationId": "application-1",
        "approvalMatchReason": "Previously selected as a strong fit.",
        "approvalSubmissionMethod": "email",
        "closing": "Wednesday, 29 July 2026",
      })
      work = {
        "runId": "approved-test",
        "claimId": "claim-1",
        "userId": "user-1",
        "subscriber": subscriber,
        "preferences": subscriber["preferences"],
        "candidates": [candidate],
        "applicationLimit": 1,
        "approvedSubmission": True,
      }
      selected = [{
        "candidate": candidate,
        "reason": "Previously selected as a strong fit.",
        "cvHtmlPath": root / "cv.html",
        "coverLetterPath": root / "letter.txt",
      }]
      existing = {**candidate, "status": "approved", "attemptCount": 0}
      with (
        mock.patch.object(agent_dispatch, "cmd_list_applications", return_value={"applications": [existing]}),
        mock.patch.object(agent_dispatch, "record_candidate_application", return_value={}) as record,
        mock.patch.object(agent_dispatch, "_prepare_documents") as prepare_documents,
        mock.patch.object(agent_dispatch, "send_application_email") as send,
        mock.patch.object(agent_dispatch, "record_channel_candidate_outcome"),
        mock.patch.object(agent_dispatch, "send_whatsapp_summary", return_value={"ok": True}),
        mock.patch.object(agent_dispatch, "publish_user_dashboard_telemetry"),
      ):
        outcome = agent_dispatch.dispatch_agent_user(
          self.args(root, "complete", claim_id="claim-1"),
          work,
          selected,
          fallback=False,
        )

      self.assertEqual(outcome["status"], "no_matches")
      self.assertEqual(record.call_args.args[2], "skipped")
      self.assertEqual(record.call_args.kwargs["notes"], "Application deadline passed before submission.")
      self.assertTrue(record.call_args.kwargs["replace"])
      prepare_documents.assert_not_called()
      send.assert_not_called()

  def test_expired_gmail_token_becomes_action_required_without_retry(self) -> None:
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
      selected = [{
        "candidate": candidate,
        "reason": "fit",
        "cvHtmlPath": root / "cv.html",
        "coverLetterPath": root / "letter.txt",
      }]
      documents = {
        "cvPdf": root / "cv.pdf",
        "coverLetterPdf": root / "letter.pdf",
        "coverLetterText": root / "letter.txt",
      }
      with (
        mock.patch.object(agent_dispatch, "cmd_list_applications", return_value={"applications": [{**candidate, "status": "failed", "attemptCount": 2}]}),
        mock.patch.object(agent_dispatch, "filter_candidates_for_user_with_stats", side_effect=lambda cands, *a, **k: (cands, {})),
        mock.patch.object(agent_dispatch, "gmail_sender_connected", return_value=True),
        mock.patch.object(agent_dispatch, "_prepare_documents", return_value=documents),
        mock.patch.object(agent_dispatch, "send_application_email", side_effect=RuntimeError('HTTP 400: "Token has been expired or revoked."')) as send,
        mock.patch.object(agent_dispatch, "record_candidate_application", return_value={}) as record,
        mock.patch.object(agent_dispatch, "record_channel_candidate_outcome"),
        mock.patch.object(agent_dispatch, "send_whatsapp_summary", return_value={"ok": True}),
        mock.patch.object(agent_dispatch, "publish_user_dashboard_telemetry"),
      ):
        outcome = agent_dispatch.dispatch_agent_user(
          self.args(root, "complete", claim_id="claim-1"), work, selected, fallback=False,
        )

      self.assertEqual(outcome["status"], "action_required")
      self.assertEqual(outcome["actionRequiredCount"], 1)
      self.assertIsNone(outcome["failureReason"])
      send.assert_called_once()
      record_args, record_kwargs = record.call_args
      self.assertEqual(record_args[2], "action_required")
      self.assertEqual(record_kwargs["blocker_code"], "gmail_reconnect_required")
      self.assertTrue(record_kwargs["replace"])

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
      strict = argparse.Namespace(
        run_id="agent-test",
        dispatch_root=root,
        require_no_failures=True,
      )
      with self.assertRaises(SystemExit):
        commands_dispatch.cmd_verify_dispatch(strict)

  def test_strict_verify_rejects_requested_summary_failure(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      work = dispatch_store.load_agent_work(root, "agent-test", claim_id)
      agent_dispatch._record_outcome(root, "agent-test", work, {
        "status": "no_matches",
        "appliedCount": 0,
        "actionRequiredCount": 0,
        "summaryDelivered": False,
        "summaryError": "gateway unavailable",
      })
      with mock.patch.object(agent_dispatch, "finalize_channel_candidate_dispatch"):
        agent_dispatch.cmd_agent_finish(self.args(root, "finish"))

      strict = argparse.Namespace(
        run_id="agent-test",
        dispatch_root=root,
        require_no_failures=True,
      )
      with self.assertRaises(SystemExit):
        commands_dispatch.cmd_verify_dispatch(strict)

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
      strict = commands_dispatch.cmd_verify_dispatch(argparse.Namespace(
        run_id="agent-test",
        dispatch_root=root,
        require_no_failures=True,
      ))
      self.assertTrue(strict["ok"])

  def test_wait_returns_immediately_when_all_claims_are_terminal(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      work = dispatch_store.load_agent_work(root, "agent-test", claim_id)
      agent_dispatch._record_outcome(root, "agent-test", work, {
        "status": "no_matches", "appliedCount": 0, "actionRequiredCount": 0,
      })
      wait_args = self.args(root, "wait")
      wait_args.after_terminal_count = 1
      wait_args.wait_timeout_seconds = 10

      with mock.patch.object(agent_dispatch.time, "sleep") as sleep:
        result = agent_dispatch.cmd_agent_wait(wait_args)

      self.assertFalse(result["timedOut"])
      self.assertEqual(result["terminalCount"], 1)
      sleep.assert_not_called()

  def test_wait_returns_when_terminal_count_advances(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      work = dispatch_store.load_agent_work(root, "agent-test", claim_id)
      wait_args = self.args(root, "wait")
      wait_args.after_terminal_count = 0
      wait_args.wait_timeout_seconds = 10

      def finish_claim(_seconds: float) -> None:
        agent_dispatch._record_outcome(root, "agent-test", work, {
          "status": "no_matches", "appliedCount": 0, "actionRequiredCount": 0,
        })

      with (
        mock.patch.object(agent_dispatch.time, "monotonic", return_value=0),
        mock.patch.object(agent_dispatch.time, "sleep", side_effect=finish_claim) as sleep,
      ):
        result = agent_dispatch.cmd_agent_wait(wait_args)

      self.assertFalse(result["timedOut"])
      self.assertEqual(result["terminalCount"], 1)
      sleep.assert_called_once()

  def test_wait_timeout_is_bounded_and_read_only(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      prepared = self.prepare(root)
      claim_id = prepared["claims"][0]["claimId"]
      before = dispatch_store.load_agent_claim(root, "agent-test", claim_id)
      wait_args = self.args(root, "wait")
      wait_args.after_terminal_count = 0
      wait_args.wait_timeout_seconds = 1

      with (
        mock.patch.object(agent_dispatch.time, "monotonic", side_effect=[0, 2]),
        mock.patch.object(agent_dispatch.time, "sleep") as sleep,
      ):
        result = agent_dispatch.cmd_agent_wait(wait_args)

      self.assertTrue(result["timedOut"])
      self.assertEqual(result["terminalCount"], 0)
      sleep.assert_not_called()
      self.assertEqual(dispatch_store.load_agent_claim(root, "agent-test", claim_id), before)


if __name__ == "__main__":
  unittest.main()
