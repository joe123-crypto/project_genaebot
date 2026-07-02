#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import job_scout


class OpenClawBinResolutionTests(unittest.TestCase):
  def test_openclaw_bin_env_override_wins(self) -> None:
    with mock.patch.dict(job_scout.os.environ, {"OPENCLAW_BIN": "/custom/openclaw"}, clear=True), \
         mock.patch.object(job_scout.shutil, "which", return_value="/usr/local/bin/openclaw"):
      self.assertEqual(job_scout.resolve_openclaw_bin(), Path("/custom/openclaw"))

  def test_openclaw_bin_uses_path_discovery(self) -> None:
    with mock.patch.dict(job_scout.os.environ, {}, clear=True), \
         mock.patch.object(job_scout.shutil, "which", return_value="/usr/local/bin/openclaw"):
      self.assertEqual(job_scout.resolve_openclaw_bin(), Path("/usr/local/bin/openclaw"))

  def test_openclaw_bin_uses_first_executable_fallback(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      first = root / "first" / "openclaw"
      second = root / "second" / "openclaw"
      first.parent.mkdir()
      second.parent.mkdir()
      first.write_text("#!/bin/sh\n", encoding="utf-8")
      second.write_text("#!/bin/sh\n", encoding="utf-8")
      first.chmod(0o755)
      second.chmod(0o755)

      with mock.patch.dict(job_scout.os.environ, {}, clear=True), \
           mock.patch.object(job_scout.shutil, "which", return_value=None):
        self.assertEqual(job_scout.resolve_openclaw_bin((first, second)), first)

  def test_openclaw_bin_falls_back_to_command_name(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      missing = Path(tmp) / "missing" / "openclaw"
      with mock.patch.dict(job_scout.os.environ, {}, clear=True), \
           mock.patch.object(job_scout.shutil, "which", return_value=None):
        self.assertEqual(job_scout.resolve_openclaw_bin((missing,)), Path("openclaw"))


class CvStagingTests(unittest.TestCase):
  def _pdf(self, root: Path) -> Path:
    source = root / "resume.pdf"
    source.write_bytes(b"%PDF-1.7 fake")
    return source

  def test_stage_cv_uploads_pdf_to_r2(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      source = self._pdf(Path(tmp))
      with mock.patch.object(job_scout, "linked_user_id", return_value="user-1"), \
           mock.patch.object(job_scout, "request_multipart", return_value={"ok": True, "key": "user-1/cv/cv.pdf"}) as upload:
        result = job_scout.stage_cv("+263 785 632 943", str(source))

      self.assertTrue(result["ok"])
      self.assertEqual(result["userId"], "user-1")
      self.assertEqual(result["cvFileRef"], "user-1/cv/cv.pdf")
      url, fields = upload.call_args.args[0], upload.call_args.args[1]
      self.assertTrue(url.endswith("/internal/job-scout/cv"))
      self.assertEqual(fields["userId"], "user-1")

  def test_stage_cv_rejects_non_pdf(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      source = Path(tmp) / "resume.docx"
      source.write_bytes(b"docx content")
      with mock.patch.object(job_scout, "linked_user_id", return_value="user-1"), \
           mock.patch.object(job_scout, "request_multipart") as upload:
        with self.assertRaises(SystemExit):
          job_scout.stage_cv("+263785632943", str(source))
      upload.assert_not_called()

  def test_save_profile_dry_run_reports_r2_key(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      source = self._pdf(Path(tmp))
      args = argparse.Namespace(
        phone="+263785632943",
        user_id="user-1",
        cv=str(source),
        storage_root=Path(tmp) / "storage",
        profile_json=None,
        target_role=["General worker"],
        location=["Harare, Zimbabwe"],
        country=None,
        language=None,
        qualification=None,
        experience=None,
        skill=None,
        exclusion=None,
        prompt_note=None,
        max_applications_per_run=None,
        no_auto_apply=False,
        dry_run=True,
      )

      result = job_scout.cmd_save_profile(args)

      self.assertTrue(result["dryRun"])
      self.assertEqual(result["cvFileRef"], "user-1/cv/cv.pdf")
      self.assertTrue(result["uploadEndpoint"].endswith("/internal/job-scout/cv"))
      self.assertEqual(result["preferences"]["targetRoles"], ["General worker"])
      self.assertEqual(result["preferences"]["country"], "zw")

  def test_resolve_cv_ref_accepts_staged_key(self) -> None:
    self.assertEqual(
      job_scout.resolve_cv_ref("user-1", "user-1/cv/cv.pdf", do_upload=True),
      "user-1/cv/cv.pdf",
    )

  def test_resolve_cv_ref_rejects_missing_local_path(self) -> None:
    # A nonexistent local path must never be persisted as an R2 key.
    with self.assertRaises(SystemExit):
      job_scout.resolve_cv_ref("user-1", "/tmp/does-not-exist-cv.pdf", do_upload=True)

  def test_resolve_cv_ref_rejects_foreign_key(self) -> None:
    with self.assertRaises(SystemExit):
      job_scout.resolve_cv_ref("user-1", "other-user/cv/cv.pdf", do_upload=False)


class CvDownloadTests(unittest.TestCase):
  def test_subscriber_cv_file_downloads_from_r2(self) -> None:
    job_scout._cv_download_cache.clear()
    subscriber = {"userId": "user-9", "whatsappPhoneHash": "hash9", "cvFileRef": "user-9/cv/cv.pdf"}
    with tempfile.TemporaryDirectory() as tmp:
      dest = Path(tmp) / "hash9" / "cv.pdf"
      with mock.patch.object(job_scout, "request_json", return_value={"url": "https://r2.example/presigned"}) as getreq, \
           mock.patch.object(job_scout, "cv_local_dest", return_value=dest), \
           mock.patch.object(job_scout.urllib.request, "urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value.read.return_value = b"%PDF-1.7 data"
        path = job_scout.subscriber_cv_file(subscriber)

      self.assertEqual(path, str(dest))
      self.assertEqual(Path(path).read_bytes(), b"%PDF-1.7 data")
      called_url = getreq.call_args.args[1]
      self.assertIn("/internal/job-scout/cv?", called_url)
      self.assertIn("userId=user-9", called_url)

  def test_cv_object_available_true_on_url(self) -> None:
    with mock.patch.object(job_scout, "api_key", return_value="k"), \
         mock.patch.object(job_scout.urllib.request, "urlopen") as urlopen:
      urlopen.return_value.__enter__.return_value.read.return_value = b'{"url": "https://r2/x"}'
      self.assertTrue(job_scout.cv_object_available("user-9"))

  def test_cv_object_available_false_on_404(self) -> None:
    err = job_scout.urllib.error.HTTPError("u", 404, "not found", {}, None)
    with mock.patch.object(job_scout, "api_key", return_value="k"), \
         mock.patch.object(job_scout.urllib.request, "urlopen", side_effect=err):
      self.assertFalse(job_scout.cv_object_available("user-9"))


class PreferredLocationTests(unittest.TestCase):
  def test_resolves_country_from_location_suffix(self) -> None:
    self.assertEqual(
      job_scout.resolve_preferred_locations(["Bindura, Zimbabwe"]),
      {
        "locations": ["Bindura, Zimbabwe"],
        "country": "zw",
        "countryName": "Zimbabwe",
      },
    )
    self.assertEqual(
      job_scout.resolve_preferred_locations(["Algiers, Algeria"])["country"],
      "dz",
    )

  def test_country_aliases_and_codes_are_supported(self) -> None:
    self.assertEqual(job_scout.resolve_preferred_locations(["London, UK"])["country"], "gb")
    self.assertEqual(job_scout.resolve_preferred_locations(["New York, US"])["country"], "us")

  def test_location_must_include_country(self) -> None:
    with self.assertRaisesRegex(SystemExit, "must include the country"):
      job_scout.resolve_preferred_locations(["Harare"])

  def test_unknown_country_is_rejected(self) -> None:
    with self.assertRaisesRegex(SystemExit, "country is not recognized"):
      job_scout.resolve_preferred_locations(["Example City, Not A Country"])

  def test_locations_must_share_one_country(self) -> None:
    with self.assertRaisesRegex(SystemExit, "must be in one country"):
      job_scout.resolve_preferred_locations(["Harare, Zimbabwe", "Algiers, Algeria"])

  def test_explicit_country_must_match_location(self) -> None:
    with self.assertRaisesRegex(SystemExit, "conflicts"):
      job_scout.resolved_profile_country(["Bindura, Zimbabwe"], "dz")

  def test_no_location_retains_algeria_default(self) -> None:
    self.assertEqual(job_scout.resolved_profile_country([]), "dz")

  def test_stored_bare_location_uses_valid_stored_country(self) -> None:
    self.assertEqual(
      job_scout.normalize_stored_locations(["Harare"], "zw"),
      {"locations": ["Harare, Zimbabwe"], "country": "zw"},
    )
    self.assertEqual(
      job_scout.normalize_stored_locations(["Algiers"], "dz"),
      {"locations": ["Algiers, Algeria"], "country": "dz"},
    )

  def test_stored_country_only_location_becomes_nationwide(self) -> None:
    self.assertEqual(
      job_scout.normalize_stored_locations(["Zimbabwe"]),
      {"locations": ["Nationwide, Zimbabwe"], "country": "zw"},
    )

  def test_stored_bare_location_without_country_is_rejected(self) -> None:
    with self.assertRaisesRegex(SystemExit, "needs a valid country"):
      job_scout.normalize_stored_locations(["Harare"])
    with self.assertRaisesRegex(SystemExit, "not recognized"):
      job_scout.normalize_stored_locations(["Harare"], "not-a-country")

  def test_conflicting_stored_country_is_rejected(self) -> None:
    with self.assertRaisesRegex(SystemExit, "conflicts"):
      job_scout.normalize_stored_locations(["Harare, Zimbabwe"], "dz")

  def test_old_and_new_locations_must_share_one_country(self) -> None:
    self.assertEqual(
      job_scout.resolve_profile_locations(["Harare"], ["Bindura, Zimbabwe"], stored_country="zw"),
      {
        "locations": ["Harare, Zimbabwe", "Bindura, Zimbabwe"],
        "country": "zw",
      },
    )
    with self.assertRaisesRegex(SystemExit, "must be in one country"):
      job_scout.resolve_profile_locations(["Harare"], ["Algiers, Algeria"], stored_country="zw")

  def test_new_bare_location_is_rejected_even_with_stored_country(self) -> None:
    with self.assertRaisesRegex(SystemExit, "must include the country"):
      job_scout.resolve_profile_locations([], ["Harare"], stored_country="zw")

  def test_save_profile_migrates_stored_location(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      cv = root / "resume.pdf"
      cv.write_bytes(b"pdf")
      base = {
        "targetRoles": ["Driver"],
        "locations": ["Harare"],
        "country": "zw",
        "skills": ["Defensive driving"],
      }
      args = argparse.Namespace(
        phone="+263785632943",
        user_id="user-1",
        cv=str(cv),
        storage_root=root / "storage",
        profile_json=json.dumps(base),
        target_role=None,
        location=None,
        country=None,
        language=None,
        qualification=None,
        experience=None,
        skill=None,
        exclusion=None,
        prompt_note=None,
        max_applications_per_run=None,
        no_auto_apply=False,
        dry_run=True,
      )

      result = job_scout.cmd_save_profile(args)

    self.assertEqual(result["preferences"]["locations"], ["Harare, Zimbabwe"])
    self.assertEqual(result["preferences"]["country"], "zw")
    self.assertEqual(result["preferences"]["skills"], ["Defensive driving"])

  def test_manual_fetch_normalizes_profile_only_in_memory(self) -> None:
    base = {"targetRoles": ["Driver"], "locations": ["Harare"], "country": "zw"}
    args = argparse.Namespace(
      profile_json=json.dumps(base),
      target_role=None,
      location=None,
      country=None,
      language=None,
      exclusion=None,
      limit=5,
    )
    with mock.patch.object(job_scout, "fetch_jsearch_candidates", return_value=[]) as jsearch, \
         mock.patch.object(job_scout, "fetch_scraped_candidates", return_value=[]) as scraped, \
         mock.patch.object(job_scout, "jsearch_provider", return_value="test"):
      result = job_scout.cmd_fetch_candidates(args)

    normalized = {"targetRoles": ["Driver"], "locations": ["Harare, Zimbabwe"], "country": "zw", "language": "fr", "exclusions": [], "maxApplicationsPerRun": 5}
    jsearch.assert_called_once_with(normalized, 5)
    scraped.assert_called_once_with(normalized, 5)
    self.assertEqual(result["queries"], ["Driver à Harare, Zimbabwe"])
    self.assertEqual(base["locations"], ["Harare"])


class JSearchTests(unittest.TestCase):
  def test_search_queries_use_roles_locations_and_default_location(self) -> None:
    self.assertEqual(
      job_scout.search_queries({"targetRoles": ["Security guard", "Driver"], "locations": ["Algiers"]}),
      ["Security guard à Algiers", "Driver à Algiers"],
    )
    self.assertEqual(
      job_scout.search_queries({"targetRoles": ["Cleaner"]}),
      ["Cleaner à Algeria"],
    )

  def test_normalized_candidate_extracts_explicit_email_only(self) -> None:
    candidate = job_scout.normalized_candidate(
      {
        "job_id": "abc123",
        "employer_name": "Example SARL",
        "job_title": "Agent commercial",
        "job_city": "Alger",
        "job_country": "DZ",
        "job_apply_link": "https://example.com/apply",
        "job_description": "Envoyez votre CV a recrutement@example.dz avant jeudi.",
      },
      "Agent commercial à Alger",
    )

    self.assertEqual(candidate["candidateId"], "abc123")
    self.assertEqual(candidate["company"], "Example SARL")
    self.assertEqual(candidate["role"], "Agent commercial")
    self.assertEqual(candidate["applicationEmail"], "recrutement@example.dz")
    self.assertEqual(candidate["sourceUrl"], "https://example.com/apply")

  def test_fetch_jsearch_candidates_uses_profile_country_and_language(self) -> None:
    with mock.patch.object(job_scout, "jsearch_request") as request:
      request.return_value = {
        "data": [
          {
            "job_id": "1",
            "employer_name": "Entreprise",
            "job_title": "Technicien",
            "job_description": "Poste ouvert.",
          }
        ]
      }

      candidates = job_scout.fetch_jsearch_candidates(
        {"targetRoles": ["Technicien"], "locations": ["Oran"], "country": "dz", "language": "fr"},
        2,
      )

    request.assert_called_once_with("Technicien à Oran", "dz", "fr", 2)
    self.assertEqual(len(candidates), 1)
    self.assertEqual(candidates[0]["company"], "Entreprise")

  def test_fetch_jsearch_candidates_soft_fails_on_outage(self) -> None:
    # A transient JSearch outage must not raise: the caller falls back to scrapers.
    with mock.patch.object(job_scout, "jsearch_request", side_effect=job_scout.JSearchUnavailable("HTTP 504")):
      candidates = job_scout.fetch_jsearch_candidates({"targetRoles": ["Driver"], "locations": ["Harare"]}, 5)
    self.assertEqual(candidates, [])
    self.assertFalse(issubclass(job_scout.JSearchUnavailable, SystemExit))

  def test_jsearch_request_maps_read_timeout_to_unavailable(self) -> None:
    # A socket read timeout (TimeoutError, not HTTPError/URLError) must still be
    # classified transient so dispatch falls back to the scraped boards.
    with mock.patch.object(job_scout, "jsearch_provider", return_value="rapidapi"), \
         mock.patch.object(job_scout, "env_default", side_effect=lambda key, default="": "key" if "KEY" in key else default), \
         mock.patch("urllib.request.urlopen", side_effect=TimeoutError("The read operation timed out")):
      with self.assertRaises(job_scout.JSearchUnavailable):
        job_scout.jsearch_request("driver", "dz", "fr", 5)

  def test_filter_candidates_skips_existing_duplicates_and_exclusions(self) -> None:
    candidates = [
      {"company": "A", "role": "Cleaner", "description": "Night shift", "sourceUrl": "https://a.test/1", "candidateId": "1"},
      {"company": "A", "role": "Cleaner", "description": "Night shift", "sourceUrl": "https://a.test/1", "candidateId": "1"},
      {"company": "B", "role": "Driver", "description": "Senior management", "sourceUrl": "https://b.test/1", "candidateId": "2"},
      {"company": "C", "role": "Guard", "description": "Day shift", "sourceUrl": "https://c.test/1", "candidateId": "3"},
    ]
    filtered = job_scout.filter_candidates(
      candidates,
      {"exclusions": ["Senior management"]},
      [{"company": "A", "role": "Cleaner"}],
      5,
    )

    self.assertEqual(filtered, [candidates[3]])

  def test_filter_retries_failed_but_not_historical_manual_records(self) -> None:
    candidates = [
      {"company": "A", "role": "Driver", "sourceUrl": "https://a.test", "candidateId": "1"},
      {"company": "B", "role": "Clerk", "sourceUrl": "https://b.test", "candidateId": "2"},
    ]
    applications = [
      {"company": "A", "role": "Driver", "status": "failed", "attemptCount": 1},
      {"company": "B", "role": "Clerk", "status": "physical_submission"},
    ]

    self.assertEqual(job_scout.filter_candidates(candidates, {}, applications, 5), [candidates[0]])

  def test_pilot_filter_applies_allowlist(self) -> None:
    subscribers = [{"userId": "one"}, {"userId": "two"}]
    with mock.patch.dict(job_scout.os.environ, {"JOB_SCOUT_PILOT_USER_IDS": "two"}):
      self.assertEqual(job_scout.restrict_to_pilot(subscribers, True), [subscribers[1]])


class JSearchDispatchTests(unittest.TestCase):
  def subscriber(self, auto_apply: bool = True) -> dict:
    return {
      "userId": "user-1",
      "whatsappPhone": "+213555010101",
      "whatsappPhoneHash": "phonehash1",
      "senderEmail": "applicant@example.com",
      "cvFileRef": "/tmp/cv.pdf",
      "profile": {"displayName": "Test Applicant"},
      "preferences": {
        "targetRoles": ["Driver"],
        "locations": ["Algiers"],
        "country": "dz",
        "language": "fr",
        "autoApply": auto_apply,
        "maxApplicationsPerRun": 5,
      },
    }

  def args(self, root: Path, dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
      run_id="unit-dispatch",
      limit_ready=10,
      max_candidates_per_user=5,
      dispatch_root=root,
      dry_run=dry_run,
      pilot_only=False,
    )

  def candidate(self, email: str | None = "hr@example.dz", link: str = "https://jobs.example/apply") -> dict:
    return {
      "candidateId": "job-1",
      "source": "jsearch",
      "sourceUrl": link,
      "applyLinks": [link] if link else [],
      "applicationEmails": [email] if email else [],
      "applicationEmail": email,
      "company": "Example Logistics",
      "role": "Driver",
      "location": "Algiers, DZ",
      "query": "Driver a Algiers",
      "closing": None,
      "description": "Delivery role.",
    }

  def dispatch_with(
    self,
    root: Path,
    candidates: list[dict],
    auto_apply: bool = True,
    send_error: Exception | None = None,
    web_result: dict | None = None,
  ) -> dict:
    subscriber = self.subscriber(auto_apply=auto_apply)
    with (
      mock.patch.object(job_scout, "cmd_list_ready", return_value={"subscribers": [subscriber]}),
      mock.patch.object(job_scout, "cmd_list_applications", return_value={"applications": []}),
      mock.patch.object(job_scout, "fetch_jsearch_candidates", return_value=candidates),
      mock.patch.object(job_scout, "jsearch_provider", return_value="rapidapi"),
      mock.patch.object(job_scout, "write_cover_letter", return_value=root / "cover-letter.txt"),
      mock.patch.object(job_scout, "web_apply_payload", return_value={"applicationUrl": "https://jobs.example/apply"}),
      mock.patch.object(
        job_scout,
        "request_web_application",
        return_value=web_result or {"outcome": "action_required", "blockerCode": "login_required"},
      ) as web_apply,
      mock.patch.object(job_scout, "send_whatsapp_summary", return_value={"ok": True}) as whatsapp,
      mock.patch.object(job_scout, "cmd_record_application", return_value={"ok": True}) as record,
    ):
      if send_error is None:
        send_patch = mock.patch.object(job_scout, "send_application_email", return_value={"messageId": "msg-1"})
      else:
        send_patch = mock.patch.object(job_scout, "send_application_email", side_effect=send_error)
      with send_patch as send:
        result = job_scout.cmd_dispatch_jsearch(self.args(root))
    result["_record_calls"] = record.call_args_list
    result["_send_calls"] = send.call_args_list
    result["_whatsapp_calls"] = whatsapp.call_args_list
    result["_web_calls"] = web_apply.call_args_list
    return result

  def test_dispatch_applies_email_candidate_and_records_outcome(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      result = self.dispatch_with(Path(tmp), [self.candidate()])

      self.assertEqual(result["appliedCount"], 1)
      self.assertEqual(result["physicalCount"], 0)
      self.assertEqual(result["failedCount"], 0)
      self.assertEqual(result["users"][0]["status"], "applied")
      self.assertEqual(result["_send_calls"][0].args[1]["applicationEmail"], "hr@example.dz")
      record_args = result["_record_calls"][0].args[0]
      self.assertEqual(record_args.status, "applied")
      self.assertEqual(record_args.source, "jsearch")

  def test_dispatch_records_action_required_when_site_needs_login(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      result = self.dispatch_with(Path(tmp), [self.candidate(email=None)])

      self.assertEqual(result["appliedCount"], 0)
      self.assertEqual(result["actionRequiredCount"], 1)
      self.assertEqual(result["physicalCount"], 0)
      self.assertEqual(result["users"][0]["status"], "action_required")
      record_args = result["_record_calls"][0].args[0]
      self.assertEqual(record_args.status, "action_required")
      self.assertEqual(record_args.blocker_code, "login_required")

  def test_dispatch_submits_website_candidate_without_email(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      result = self.dispatch_with(
        Path(tmp),
        [self.candidate(email=None)],
        web_result={
          "outcome": "submitted",
          "confirmation": "Application submitted",
          "evidence": {"finalUrl": "https://jobs.example/done", "filledFields": ["email", "cv"]},
        },
      )

      self.assertEqual(result["appliedCount"], 1)
      self.assertEqual(result["actionRequiredCount"], 0)
      record_args = result["_record_calls"][0].args[0]
      self.assertEqual(record_args.status, "applied")
      self.assertEqual(record_args.submission_method, "website")

  def test_dispatch_records_no_matches(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      result = self.dispatch_with(Path(tmp), [])

      self.assertEqual(result["noMatchesCount"], 1)
      self.assertEqual(result["users"][0]["status"], "no_matches")
      self.assertEqual(result["_record_calls"], [])

  def test_dispatch_failed_send_records_failed_sanitized_note(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      result = self.dispatch_with(
        Path(tmp),
        [self.candidate()],
        send_error=RuntimeError('Agent Genaie mail bridge failed with HTTP 400: {"ok":false,"error":"Must specify senderKey, senderUid, or useOwnerAuth=true."} at /home/joseph/private'),
      )

      self.assertEqual(result["failedCount"], 1)
      self.assertEqual(result["users"][0]["status"], "failed")
      record_args = result["_record_calls"][0].args[0]
      self.assertEqual(record_args.status, "failed")
      self.assertNotIn("/home/joseph/private", record_args.notes)
      public_message = result["_whatsapp_calls"][0].args[1]
      self.assertIn("service failed", public_message)
      for value in ("HTTP 400", "senderKey", "senderUid", "useOwnerAuth", '{"ok":false}', "/home/joseph"):
        self.assertNotIn(value, public_message)

  def test_send_application_email_uses_subscriber_uid_sender_selector(self) -> None:
    subscriber = self.subscriber()
    candidate = self.candidate()
    with mock.patch.object(job_scout, "subscriber_cv_file", return_value="/tmp/cv.pdf"), \
         mock.patch.object(job_scout, "run_json_command", return_value={"messageId": "msg-1"}) as run:
      result = job_scout.send_application_email(subscriber, candidate, Path("/tmp/cover-letter.txt"))

    self.assertEqual(result["messageId"], "msg-1")
    command = run.call_args.args[0]
    self.assertIn("--from", command)
    self.assertIn("applicant@example.com", command)
    self.assertIn("--sender-uid", command)
    self.assertEqual(command[command.index("--sender-uid") + 1], "user-1")

  def test_send_whatsapp_summary_uses_gateway_state_and_token(self) -> None:
    with mock.patch.dict(job_scout.os.environ, {
      "OPENCLAW_HOME": "/home/joseph/.openclaw",
      "GATEWAY_AUTH_TOKEN": "gateway-token",
      "KEEP_ME": "yes",
    }, clear=True), mock.patch.object(
      job_scout,
      "run_json_command",
      return_value={"messageId": "wa-1"},
    ) as run:
      result = job_scout.send_whatsapp_summary("+263700000000", "Job Scout update")

    self.assertEqual(result["messageId"], "wa-1")
    command_env = run.call_args.kwargs["env"]
    self.assertNotIn("OPENCLAW_HOME", command_env)
    self.assertEqual(command_env["OPENCLAW_GATEWAY_TOKEN"], "gateway-token")
    self.assertEqual(command_env["KEEP_ME"], "yes")

  def test_non_zw_subscriber_gets_no_board_sources(self) -> None:
    # The Algeria subscriber (country dz) must never receive Zimbabwe board candidates;
    # fetch_scraped_candidates is gated by zw_targeted. This guards the geo gate.
    board_markers = ["vacancymail", "iharare", "vacancybox"]
    with tempfile.TemporaryDirectory() as tmp:
      with mock.patch("scrapers.fetch_scraped_candidates") as inner:
        result = self.dispatch_with(Path(tmp), [self.candidate()])
      # The dz subscriber is gated out, so the scraper package is never invoked.
      inner.assert_not_called()

    text = json.dumps(result, default=str).casefold()
    for marker in board_markers:
      self.assertNotIn(marker, text)


class ZwGatingTests(unittest.TestCase):
  def test_zw_targeted_by_country_code(self) -> None:
    self.assertTrue(job_scout.zw_targeted({"country": "zw"}))
    self.assertTrue(job_scout.zw_targeted({"country": "Zimbabwe"}))

  def test_zw_targeted_by_location(self) -> None:
    self.assertTrue(job_scout.zw_targeted({"country": "dz", "locations": ["Harare"]}))
    self.assertTrue(job_scout.zw_targeted({"locations": ["Bulawayo CBD"]}))

  def test_not_zw_targeted_for_algeria(self) -> None:
    self.assertFalse(job_scout.zw_targeted({"country": "dz", "locations": ["Algiers"]}))

  def test_fetch_scraped_candidates_skips_non_zw(self) -> None:
    with mock.patch("scrapers.fetch_scraped_candidates") as inner:
      out = job_scout.fetch_scraped_candidates({"country": "dz", "locations": ["Oran"]}, 5)
    self.assertEqual(out, [])
    inner.assert_not_called()

  def test_fetch_scraped_candidates_runs_for_zw(self) -> None:
    sentinel = [{"source": "vacancymail", "company": "A", "role": "Driver"}]
    with mock.patch("scrapers.fetch_scraped_candidates", return_value=sentinel) as inner:
      out = job_scout.fetch_scraped_candidates({"country": "zw", "targetRoles": ["Driver"]}, 5)
    self.assertEqual(out, sentinel)
    inner.assert_called_once()

  def test_fetch_scraped_candidates_swallows_errors(self) -> None:
    with mock.patch("scrapers.fetch_scraped_candidates", side_effect=RuntimeError("boom")):
      out = job_scout.fetch_scraped_candidates({"country": "zw", "targetRoles": ["Driver"]}, 5)
    self.assertEqual(out, [])


class ScraperBaseTests(unittest.TestCase):
  def test_build_candidate_matches_normalized_schema_keys(self) -> None:
    from scrapers import base

    normalized = job_scout.normalized_candidate(
      {"job_title": "Driver", "employer_name": "Acme", "job_apply_link": "https://x/apply"},
      "Driver",
    )
    candidate = base.build_candidate(
      "vacancymail", role="Driver", company="Acme", url="https://x/job",
      description="Drive", location="Harare", emails=["hr@acme.co.zw"],
    )
    self.assertEqual(set(candidate.keys()), set(normalized.keys()))
    self.assertEqual(candidate["source"], "vacancymail")
    self.assertEqual(candidate["applicationEmail"], "hr@acme.co.zw")
    self.assertIn("https://x/job", candidate["applyLinks"])

  def test_role_matcher_requires_all_significant_words(self) -> None:
    from scrapers import base

    match = base.build_role_matcher(["Delivery Driver", "Security Guard"])
    self.assertTrue(match("Delivery Driver", "vans in Harare"))
    self.assertTrue(match("Guard", "security guard for site"))
    self.assertFalse(match("Accountant", "manage ledgers"))

  def test_empty_roles_match_everything(self) -> None:
    from scrapers import base

    self.assertTrue(base.build_role_matcher([])("Anything", "text"))


class BoardParserTests(unittest.TestCase):
  VM_LIST = (
    '<a href="/jobs/driver-101/" class="job-listing">'
    '<div class="job-listing-description">'
    '<h3 class="job-listing-title">Delivery Driver</h3>'
    '<h4 class="job-listing-company">Acme Logistics</h4>'
    '<p class="job-listing-text">Driver needed for deliveries in Harare.</p>'
    '</div>'
    '<div class="job-listing-footer"><ul>'
    '<li><i class="icon-material-outline-location-on"></i> Harare</li>'
    '<li><i class="icon-material-outline-access-time"></i> Expires 30 Jun 2026</li>'
    '</ul></div></a>'
  )
  VM_DETAIL = (
    '<div class="col-xl-8 col-lg-8 content-right-offset">'
    '<div class="single-page-section"><h3>Job Description</h3><p>Drive vans across Harare.</p></div>'
    '<div class="single-page-section"><h3>How to Apply</h3><p>Email: jobs@acme.co.zw before 30 June.</p></div>'
    '</div>'
    '<div class="col-xl-4"><a href="mailto:footer@vacancymail.co.zw">noise</a></div>'
  )
  IH_LIST = (
    '<div class="company-list-logo"><a href="/job/nurse-200/"><img src="x" alt="City Hospital"></a></div>'
    '<div class="company-list-details">'
    '<h3><a href="/job/nurse-200/">Registered Nurse</a></h3>'
    '<p class="company-state"><i class="fa fa-map-marker"></i> Bulawayo</p>'
    '<p class="open-icon"><i class="fa fa-clock-o"></i> Expires 25 Jun 2026</p>'
    '<p class="company-state"><i class="fa fa-building"></i> City Hospital</p>'
    '</div>'
  )
  IH_DETAIL = (
    '<div class="single-candidate-bottom-left"><h3>job Description</h3>'
    '<p>Care for patients. Apply via hr@cityhospital.co.zw</p>'
    '<a href="https://apply.cityhospital.co.zw/n">Apply Now</a></div>'
    '<div class="single-candidate-bottom-right">related jobs</div>'
  )
  VB_ITEM = {
    "title": {"rendered": "Cashier"},
    "link": "https://vacancybox.co.zw/job/cashier-9/",
    "date": "2026-06-16T10:00:00",
    "content": {"rendered": "<p>Sponsored DUE: 25 JUN 2026 Cashier at Acme. Email hr@acme.co.zw</p>"},
  }
  VB_DETAIL = (
    '<div class="company"><img class="company_logo" alt="Acme"/>'
    '<div class="company_header"><p class="name"><strong>Acme Retail</strong></p></div></div>'
    '<li class="location"><a class="google_map_link" href="#">Mutare</a></li>'
  )

  def test_vacancymail_parses_card_and_detail(self) -> None:
    from scrapers import vacancymail as vm

    cards = vm.parse_cards(self.VM_LIST)
    self.assertEqual(len(cards), 1)
    self.assertEqual(cards[0]["role"], "Delivery Driver")
    self.assertEqual(cards[0]["company"], "Acme Logistics")
    self.assertEqual(cards[0]["location"], "Harare")
    self.assertEqual(cards[0]["closing"], "30 Jun 2026")
    self.assertEqual(cards[0]["url"], "https://vacancymail.co.zw/jobs/driver-101/")
    desc, emails = vm.parse_detail(self.VM_DETAIL)
    self.assertIn("Drive vans", desc)
    self.assertEqual(emails, ["jobs@acme.co.zw"])  # footer e-mail excluded

  def test_vacancymail_fetch_with_match(self) -> None:
    from scrapers import vacancymail as vm

    def fake_get(url, timeout=25):
      return self.VM_LIST if url == vm.LIST_URL else self.VM_DETAIL

    with mock.patch("scrapers.base.http_get", side_effect=fake_get):
      out = vm.fetch(limit=5, match=lambda role, text: "driver" in f"{role} {text}".lower())
    self.assertEqual(len(out), 1)
    self.assertEqual(out[0]["source"], "vacancymail")
    self.assertEqual(out[0]["applicationEmail"], "jobs@acme.co.zw")

  def test_iharare_parses_card_and_detail(self) -> None:
    from scrapers import iharare as ih

    cards = ih.parse_cards(self.IH_LIST)
    self.assertEqual(len(cards), 1)
    self.assertEqual(cards[0]["role"], "Registered Nurse")
    self.assertEqual(cards[0]["company"], "City Hospital")
    self.assertEqual(cards[0]["location"], "Bulawayo")
    desc, emails, links = ih.parse_detail(self.IH_DETAIL)
    self.assertIn("Care for patients", desc)
    self.assertEqual(emails, ["hr@cityhospital.co.zw"])
    self.assertEqual(links, ["https://apply.cityhospital.co.zw/n"])

  def test_vacancybox_item_and_detail(self) -> None:
    from scrapers import vacancybox as vb

    candidate = vb.candidate_from_item(self.VB_ITEM, timeout=5, fetch_detail=False)
    self.assertEqual(candidate["role"], "Cashier")
    self.assertEqual(candidate["applicationEmail"], "hr@acme.co.zw")
    self.assertEqual(candidate["closing"], "25 JUN 2026")
    company, location = vb.parse_detail(self.VB_DETAIL)
    self.assertEqual(company, "Acme Retail")
    self.assertEqual(location, "Mutare")

  def test_vacancybox_fetch_filters_by_role(self) -> None:
    from scrapers import vacancybox as vb

    items = [self.VB_ITEM, {"title": {"rendered": "Welder"}, "link": "https://vacancybox.co.zw/job/welder-1/",
                            "content": {"rendered": "<p>Welder wanted</p>"}}]
    with mock.patch("scrapers.base.http_get_json", return_value=items), \
         mock.patch("scrapers.base.http_get", return_value=self.VB_DETAIL):
      out = vb.fetch(limit=5, match=lambda role, text: "cashier" in f"{role} {text}".lower())
    self.assertEqual([c["role"] for c in out], ["Cashier"])
    self.assertEqual(out[0]["company"], "Acme Retail")


class ScraperIntegrationTests(unittest.TestCase):
  def test_matches_roles_uses_role_and_description(self) -> None:
    import scrapers

    self.assertTrue(scrapers.matches_roles(
      {"role": "Cleaner", "description": "office cleaning"}, {"targetRoles": ["Cleaner"]}))
    self.assertFalse(scrapers.matches_roles(
      {"role": "Welder", "description": "metal work"}, {"targetRoles": ["Cleaner"]}))

  def test_merge_dedups_jsearch_and_board_duplicate(self) -> None:
    from scrapers import base

    jsearch = job_scout.normalized_candidate(
      {"job_title": "Driver", "employer_name": "Acme", "job_apply_link": "https://acme/apply"}, "Driver")
    board = base.build_candidate("vacancymail", role="Driver", company="Acme",
                                 url="https://vacancymail.co.zw/jobs/driver-1/", description="drive")
    unique = base.build_candidate("iharare", role="Welder", company="Other",
                                  url="https://ihararejobs.com/job/welder-1/", description="weld")
    merged = job_scout.filter_candidates([jsearch, board, unique], {}, [], 10)
    keys = {job_scout.application_key(c) for c in merged}
    # Acme/Driver collapses to one entry; Other/Welder stays.
    self.assertEqual(len(merged), 2)
    self.assertIn(("acme", "driver"), keys)

  def test_record_application_propagates_board_source(self) -> None:
    candidate = {"company": "Acme", "role": "Driver", "applicationEmail": "hr@acme.co.zw",
                 "sourceUrl": "https://vacancymail.co.zw/jobs/driver-1/", "source": "vacancymail"}
    with mock.patch.object(job_scout, "request_json", return_value={"ok": True}) as req:
      job_scout.record_candidate_application("user-1", candidate, "applied")
    payload = req.call_args.args[2]
    self.assertEqual(payload["source"], "vacancymail")


if __name__ == "__main__":
  unittest.main()
