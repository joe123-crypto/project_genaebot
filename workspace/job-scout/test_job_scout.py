#!/usr/bin/env python3

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error

import job_scout
import jobscout.api
import jobscout.constants
import jobscout.delivery
import jobscout.dispatch_store
import jobscout.group_feed
import jobscout.subscriber

import jobscout.commands_group_feed
import jobscout.commands_setup
import jobscout.dispatch_runner


class ApprovedQueueTests(unittest.TestCase):
  def test_approved_count_uses_one_bounded_global_query(self) -> None:
    response = {
      "applications": [
        {"userId": "user-a", "status": "approved"},
        {"userId": "user-a", "status": "approved"},
        {"userId": "user-b", "status": "approved"},
      ],
    }
    with mock.patch.object(jobscout.commands_setup, "endpoint", side_effect=lambda value: value), \
         mock.patch.object(jobscout.commands_setup.api, "request_json", return_value=response) as request:
      result = jobscout.commands_setup.cmd_count_approved_applications(argparse.Namespace(limit=500))

    self.assertEqual(result["approvedCount"], 3)
    self.assertEqual(result["userCount"], 2)
    request.assert_called_once_with(
      "GET",
      "/internal/job-scout/applications?status=approved&limit=100",
    )


class OpenClawBinResolutionTests(unittest.TestCase):
  def test_openclaw_bin_env_override_wins(self) -> None:
    with mock.patch.dict(job_scout.os.environ, {"OPENCLAW_BIN": "/custom/openclaw"}, clear=True), \
         mock.patch.object(jobscout.api.shutil, "which", return_value="/usr/local/bin/openclaw"):
      self.assertEqual(jobscout.api.resolve_openclaw_bin(), Path("/custom/openclaw"))

  def test_openclaw_bin_uses_path_discovery(self) -> None:
    with mock.patch.dict(job_scout.os.environ, {}, clear=True), \
         mock.patch.object(jobscout.api.shutil, "which", return_value="/usr/local/bin/openclaw"):
      self.assertEqual(jobscout.api.resolve_openclaw_bin(), Path("/usr/local/bin/openclaw"))

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
           mock.patch.object(jobscout.api.shutil, "which", return_value=None):
        self.assertEqual(jobscout.api.resolve_openclaw_bin((first, second)), first)

  def test_openclaw_bin_falls_back_to_command_name(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      missing = Path(tmp) / "missing" / "openclaw"
      with mock.patch.dict(job_scout.os.environ, {}, clear=True), \
           mock.patch.object(jobscout.api.shutil, "which", return_value=None):
        self.assertEqual(jobscout.api.resolve_openclaw_bin((missing,)), Path("openclaw"))


class CvStagingTests(unittest.TestCase):
  def _pdf(self, root: Path) -> Path:
    source = root / "resume.pdf"
    source.write_bytes(b"%PDF-1.7 fake")
    return source

  def test_stage_cv_uploads_pdf_to_r2(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      source = self._pdf(Path(tmp))
      with mock.patch("jobscout.api.linked_user_id", return_value="user-1"), \
           mock.patch("jobscout.api.request_multipart", return_value={"ok": True, "key": "user-1/cv/cv.pdf"}) as upload:
        result = jobscout.api.stage_cv("+263 785 632 943", str(source))

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
      with mock.patch("jobscout.api.linked_user_id", return_value="user-1"), \
           mock.patch("jobscout.api.request_multipart") as upload:
        with self.assertRaises(SystemExit):
          jobscout.api.stage_cv("+263785632943", str(source))
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
        profile_confirmed=True,
        safety_acknowledged=True,
        dry_run=True,
      )

      result = job_scout.cmd_save_profile(args)

      self.assertTrue(result["dryRun"])
      self.assertEqual(result["cvFileRef"], "user-1/cv/pending/original.pdf")
      self.assertTrue(result["uploadEndpoint"].endswith("/internal/job-scout/cv"))
    self.assertEqual(result["preferences"]["targetRoles"], ["General worker"])
    self.assertEqual(result["preferences"]["country"], "zw")
    self.assertEqual(result["preferences"]["maxApplicationsPerRun"], 1)
    self.assertTrue(result["profileConfirmed"])
    self.assertTrue(result["safetyAcknowledged"])

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


class CheckRuntimeTests(unittest.TestCase):
  def test_check_runtime_probes_only_html_ready_subscribers(self) -> None:
    migrated = {"userId": "user-html", "cvHtmlRef": "user-html/cv/cv.html", "cvConversionStatus": "ready"}
    legacy = {"userId": "user-pdf", "cvFileRef": "user-pdf/cv/cv.pdf", "cvConversionStatus": ""}
    with tempfile.TemporaryDirectory() as tmp:
      with mock.patch.object(jobscout.commands_setup.api, "pending_links_path", return_value=Path(tmp) / "pending.json"), \
           mock.patch.object(jobscout.commands_setup, "api_key", return_value="k"), \
           mock.patch.object(jobscout.commands_setup, "RUNTIME_ROOT", Path(tmp) / "runtime"), \
           mock.patch.object(jobscout.commands_setup, "cmd_list_ready", return_value={"subscribers": [migrated, legacy]}), \
           mock.patch.object(jobscout.commands_setup.api, "cv_object_available", return_value=True) as probe:
        result = jobscout.commands_setup.cmd_check_runtime(
          argparse.Namespace(check_browser=False, check_subscribers=True)
        )

    self.assertTrue(result["ok"])
    self.assertEqual(result["subscriberCount"], 2)
    self.assertEqual(result["htmlReadyCount"], 1)
    self.assertEqual(result["accessibleCvCount"], 1)
    probe.assert_called_once_with("user-html")


class CvDownloadTests(unittest.TestCase):
  def test_subscriber_cv_file_downloads_from_r2(self) -> None:
    jobscout.subscriber._cv_download_cache.clear()
    subscriber = {"userId": "user-9", "whatsappPhoneHash": "hash9", "cvFileRef": "user-9/cv/cv.pdf"}
    with tempfile.TemporaryDirectory() as tmp:
      dest = Path(tmp) / "hash9" / "cv.pdf"
      with mock.patch("jobscout.api.request_json", return_value={"url": "https://r2.example/presigned"}) as getreq, \
           mock.patch("jobscout.subscriber.cv_local_dest", return_value=dest), \
           mock.patch.object(job_scout.urllib.request, "urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value.read.return_value = b"%PDF-1.7 data"
        path = job_scout.subscriber_cv_file(subscriber)

      self.assertEqual(path, str(dest))
      self.assertEqual(Path(path).read_bytes(), b"%PDF-1.7 data")
      called_url = getreq.call_args.args[1]
      self.assertIn("/internal/job-scout/cv?", called_url)
      self.assertIn("userId=user-9", called_url)

  def test_cv_object_available_true_on_url(self) -> None:
    with mock.patch("jobscout.api.api_key", return_value="k"), \
         mock.patch.object(job_scout.urllib.request, "urlopen") as urlopen:
      urlopen.return_value.__enter__.return_value.read.return_value = b'{"url": "https://r2/x"}'
      self.assertTrue(jobscout.api.cv_object_available("user-9"))

  def test_cv_object_available_false_on_404(self) -> None:
    err = job_scout.urllib.error.HTTPError("u", 404, "not found", {}, None)
    with mock.patch("jobscout.api.api_key", return_value="k"), \
         mock.patch.object(job_scout.urllib.request, "urlopen", side_effect=err):
      self.assertFalse(jobscout.api.cv_object_available("user-9"))

  def test_download_retries_temporarily_missing_presigned_url(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      dest = Path(tmp) / "cv.pdf"
      with mock.patch(
        "jobscout.api.request_json",
        side_effect=[{}, {}, {"url": "https://r2.example/presigned"}],
      ) as getreq, mock.patch.object(job_scout.time, "sleep") as sleep, \
           mock.patch.object(job_scout.urllib.request, "urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value.read.return_value = b"%PDF-1.7 data"
        result = jobscout.api.download_cv("user-9", dest)

    self.assertEqual(result, dest)
    self.assertEqual(getreq.call_count, 3)
    self.assertEqual(sleep.call_count, 2)

  def test_download_fails_after_bounded_missing_url_retries(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      with mock.patch("jobscout.api.request_json", return_value={}) as getreq, \
           mock.patch.object(job_scout.time, "sleep") as sleep:
        with self.assertRaisesRegex(RuntimeError, "CV is unavailable"):
          jobscout.api.download_cv("user-9", Path(tmp) / "cv.pdf")

    self.assertEqual(getreq.call_count, job_scout.CV_URL_LOOKUP_ATTEMPTS)
    self.assertEqual(sleep.call_count, job_scout.CV_URL_LOOKUP_ATTEMPTS - 1)


class JobScoutStatusTests(unittest.TestCase):
  def test_status_uses_requester_scoped_backend_endpoint(self) -> None:
    expected = {
      "configured": True,
      "linked": True,
      "gmailConnected": True,
      "senderEmail": "requester@example.com",
      "cvAvailable": True,
      "ready": True,
      "preferences": {"targetRoles": ["Driver"], "locations": ["Nationwide, Zimbabwe"]},
    }
    with mock.patch("jobscout.api.request_json", return_value=expected) as request:
      result = job_scout.cmd_status(argparse.Namespace(phone="+213 555 010 101"))

    self.assertEqual(result, expected)
    method, url = request.call_args.args
    self.assertEqual(method, "GET")
    self.assertIn("/internal/job-scout/status?", url)
    self.assertIn("phone=%2B213555010101", url)

  def test_status_does_not_query_the_multi_user_subscriber_feed(self) -> None:
    with mock.patch("jobscout.api.request_json", return_value={"configured": False}) as request, \
         mock.patch.object(job_scout, "cmd_list_ready") as list_ready:
      self.assertEqual(
        job_scout.cmd_status(argparse.Namespace(phone="+213555010101")),
        {"configured": False},
      )
    request.assert_called_once()
    list_ready.assert_not_called()


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
        profile_confirmed=True,
        safety_acknowledged=True,
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
    with mock.patch.object(jobscout.commands_setup, "fetch_jsearch_candidates", return_value=[]) as jsearch, \
         mock.patch.object(jobscout.commands_setup, "fetch_scraped_candidates", return_value=[]) as scraped, \
         mock.patch.object(jobscout.commands_setup, "jsearch_provider", return_value="test"):
      result = job_scout.cmd_fetch_candidates(args)

    normalized = {"targetRoles": ["Driver"], "locations": ["Harare, Zimbabwe"], "country": "zw", "language": "en", "exclusions": [], "maxApplicationsPerRun": 5}
    jsearch.assert_called_once_with(normalized, 5)
    scraped.assert_called_once_with(normalized, 5)
    self.assertEqual(result["queries"], ["Driver in Harare, Zimbabwe"])
    self.assertEqual(base["locations"], ["Harare"])

class CvConversionTests(unittest.TestCase):
  def test_cv_html_document_builds_semantic_page(self) -> None:
    text = "Jane Doe\nHarare, Zimbabwe\n\nEXPERIENCE\n\nDriver at Acme\n2019-2024\n\nSKILLS:\n\nDefensive driving\n"
    html = jobscout.commands_setup.cv_html_document(text)
    self.assertIn('<h1 data-cv-field="identity">Jane Doe</h1>', html)
    self.assertIn("<h2>Experience</h2>", html)
    self.assertIn("<h2>Skills</h2>", html)
    self.assertIn("Driver at Acme", html)
    self.assertIn("<title>Jane Doe - CV</title>", html)
    self.assertIn('class="cv-grid"', html)
    self.assertIn('class="sidebar"', html)

  def test_cv_html_document_drops_browser_print_headers_and_footers(self) -> None:
    text = "5/19/26, 3:47 PM\nJane Doe\n\nPROFILE\n\nReliable worker.\nfile:///home/jane/cv.html\n1/2\n"
    html = jobscout.commands_setup.cv_html_document(text)
    self.assertIn("Jane Doe", html)
    self.assertNotIn("5/19/26", html)
    self.assertNotIn("file://", html)
    self.assertNotIn(">1/2<", html)

  def test_cv_html_document_escapes_markup(self) -> None:
    html = jobscout.commands_setup.cv_html_document("Jane <Doe>\n\nC# & <b>SQL</b> work\n")
    self.assertIn("Jane &lt;Doe&gt;", html)
    self.assertIn("C# &amp; &lt;b&gt;SQL&lt;/b&gt; work", html)
    self.assertNotIn("<b>", html)

  def test_cv_html_document_rejects_empty_text(self) -> None:
    with self.assertRaises(RuntimeError):
      jobscout.commands_setup.cv_html_document("  \n\n  ")

  def test_base_cv_is_unstyled_and_style_helper_adds_one_stylesheet(self) -> None:
    text = "Jane Doe\nHarare, Zimbabwe\n\nSKILLS\n\nDefensive driving\n"
    base = jobscout.commands_setup.cv_html_document(text)
    self.assertNotIn("<style>", base)
    self.assertIn('data-cv-template="professional-v2"', base)
    styled = jobscout.commands_setup.style_cv_html(base)
    self.assertEqual(styled.count("<style>"), 1)
    # style_cv_html is idempotent-safe: a second pass adds nothing.
    self.assertEqual(jobscout.commands_setup.style_cv_html(styled).count("<style>"), 1)

  def test_style_cv_html_injects_modern_markers(self) -> None:
    base = jobscout.commands_setup.cv_html_document("Jane Doe\n\nSKILLS\n\nDriving\n")
    styled = jobscout.commands_setup.style_cv_html(base)
    self.assertIn(".skill", styled)
    self.assertIn("\\25b8", styled)  # blue arrow bullet character in CSS
    self.assertIn("</head>", styled.split("<body>", 1)[0])

  def test_decolumn_reorders_two_column_layout_into_per_column_reading_order(self) -> None:
    rows = [
      ("CONTACT", "PROFESSIONAL PROFILE"),
      ("Address", "Highly driven and adaptable"),
      ("449 Village", "individual with a strong"),
      ("Phone", "academic foundation in maths."),
      ("+263 78 000", ""),
      ("Email", "EDUCATION"),
      ("me@example.com", "Advanced Level"),
      ("SKILLS", "Chipadze High School"),
      ("Computer Literacy", "8 subjects incl maths"),
      ("Mathematics", ""),
    ]
    layout = "\n".join(f"{left:<31}{right}".rstrip() for left, right in rows)
    out = jobscout.commands_setup._decolumn_layout_text(layout)
    self.assertIsNotNone(out)
    # The whole left column (sidebar) precedes the whole right column (main),
    # so interleaved lines are restored to per-column reading order.
    self.assertLess(out.index("Computer Literacy"), out.index("PROFESSIONAL PROFILE"))
    self.assertLess(out.index("me@example.com"), out.index("Highly driven"))
    self.assertLess(out.index("Mathematics"), out.index("EDUCATION"))

  def test_decolumn_leaves_single_column_text_untouched(self) -> None:
    single = "\n".join([
      "Jane Alexandra Doe the applicant for this role",
      "PROFESSIONAL PROFILE SECTION HEADING LINE HERE",
      "A short summary describing the candidate broadly",
      "EXPERIENCE AND EMPLOYMENT HISTORY SECTION TITLE",
      "Did many useful things at several different places",
      "Built and maintained assorted internal software",
      "SKILLS AND COMPETENCIES LISTED IN THIS SECTION",
      "Python and SQL and a range of other technologies",
    ])
    self.assertIsNone(jobscout.commands_setup._decolumn_layout_text(single))

  def test_artifact_strippers_remove_browser_chrome_and_lone_arrows(self) -> None:
    text = (
      "5/19/26, 3:47 PM\n"
      "Jane Doe\n"
      "file:///home/joseph/snap/chromium/common/cv.html\n"
      "1/2\n\n"
      "ADDITIONAL INFORMATION\n\n"
      "▸ Willing to relocate\n"
      "▸\n"
      "▸ Clean record\n"
    )
    html = jobscout.commands_setup.cv_html_document(text)
    self.assertIn("Jane Doe", html)
    self.assertNotIn("5/19/26", html)
    self.assertNotIn("file://", html)
    self.assertNotIn(">1/2<", html)
    self.assertIn("<li>Willing to relocate</li>", html)
    self.assertIn("<li>Clean record</li>", html)
    # The lone arrow marker never survives as its own bullet.
    self.assertNotIn("<li></li>", html)
    self.assertNotIn("<li>▸</li>", html)

  def test_pending_conversion_subscribers_ignores_ready_records(self) -> None:
    payload = {"subscribers": [
      {"userId": "user-ready", "cvConversionStatus": "ready"},
      {"userId": "user-pending", "cvConversionStatus": "pending"},
    ]}
    with mock.patch.object(jobscout.api, "request_json", return_value=payload):
      subscribers = jobscout.commands_setup.pending_conversion_subscribers(100)
    self.assertEqual([s["userId"] for s in subscribers], ["user-pending"])

  def test_convert_pending_cvs_converts_and_survives_one_failure(self) -> None:
    payload = {"subscribers": [
      {"userId": "user-good", "cvConversionStatus": "pending", "whatsappPhone": "+263700000001"},
      {"userId": "user-bad", "cvConversionStatus": "pending", "whatsappPhone": "+263700000002"},
    ]}

    def fake_download(user_id, dest, format="html"):
      if user_id == "user-bad":
        raise RuntimeError("subscriber CV is unavailable; re-upload is required")
      dest.write_bytes(b"%PDF-fake")
      return dest

    def fake_convert(pdf_path, html_path):
      html_path.write_text("<!doctype html><html></html>", encoding="utf-8")
      return html_path

    args = argparse.Namespace(limit=100, user_id=None, dry_run=False)
    with mock.patch.object(jobscout.api, "request_json", return_value=payload), \
         mock.patch.object(jobscout.api, "download_cv", side_effect=fake_download), \
         mock.patch.object(jobscout.commands_setup, "convert_pdf_to_cv_html", side_effect=fake_convert), \
         mock.patch.object(jobscout.api, "upload_cv_html", return_value={"ok": True, "ready": True}) as upload:
      result = jobscout.commands_setup.cmd_convert_pending_cvs(args)

    self.assertFalse(result["ok"])
    self.assertEqual(result["pendingCount"], 2)
    self.assertEqual(result["convertedCount"], 1)
    self.assertEqual(result["failedCount"], 1)
    statuses = {entry["whatsappPhone"]: entry["status"] for entry in result["results"]}
    self.assertEqual(statuses["+263700000001"], "converted")
    self.assertEqual(statuses["+263700000002"], "failed")
    upload.assert_called_once()

  def test_convert_pending_cvs_dry_run_touches_nothing(self) -> None:
    payload = {"subscribers": [{"userId": "user-pending", "cvConversionStatus": "pending"}]}
    args = argparse.Namespace(limit=100, user_id=None, dry_run=True)
    with mock.patch.object(jobscout.api, "request_json", return_value=payload), \
         mock.patch.object(jobscout.api, "download_cv") as download, \
         mock.patch.object(jobscout.api, "upload_cv_html") as upload:
      result = jobscout.commands_setup.cmd_convert_pending_cvs(args)
    self.assertEqual(result["results"][0]["status"], "dry_run")
    download.assert_not_called()
    upload.assert_not_called()


class CanonicalCvRepairTests(unittest.TestCase):
  def subscriber(self) -> dict:
    return {
      "userId": "user-1",
      "whatsappPhone": "+213563719936",
      "profile": {"displayName": "Joseph Munemo"},
    }

  def test_repair_cv_backs_up_and_verifies_the_replacement(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      source = root / "source.html"
      source.write_text(
        "<!doctype html><html><body><h1>Joseph Munemo</h1>"
        "<p>joseph@example.com +213 563 719 936</p><p>Work 2020 to 2024</p></body></html>",
        encoding="utf-8",
      )
      canonical = (
        "<!doctype html><html><body><h1>Joseph Munemo</h1>"
        "<p>joseph@example.com +213 563 719 936</p><p>Work 2020 to 2024</p>"
        "<p>file:///home/joseph/cv.html</p></body></html>"
      )
      uploaded = False

      def download(_user_id, destination, format="html"):
        Path(destination).write_text(source.read_text(encoding="utf-8") if uploaded else canonical, encoding="utf-8")
        return Path(destination)

      def upload(_user_id, upload_source):
        nonlocal uploaded
        self.assertEqual(Path(upload_source), source)
        uploaded = True
        return {"ok": True}

      args = argparse.Namespace(
        phone="+213563719936",
        source_html=str(source),
        backup_dir=str(root / "backup"),
        apply=True,
      )
      with (
        mock.patch.object(jobscout.commands_setup, "cmd_list_ready", return_value={"subscribers": [self.subscriber()]}),
        mock.patch.object(jobscout.api, "download_cv", side_effect=download),
        mock.patch.object(jobscout.api, "upload_cv_html", side_effect=upload) as upload_mock,
      ):
        result = jobscout.commands_setup.cmd_repair_cv(args)

      backup_dir = Path(result["backupDirectory"])
      self.assertFalse(result["dryRun"])
      self.assertEqual(result["replacementSha256"], result["verifiedSha256"])
      self.assertEqual((backup_dir / "canonical-before.html").stat().st_mode & 0o777, 0o600)
      self.assertEqual((backup_dir / "manifest.json").stat().st_mode & 0o777, 0o600)
      upload_mock.assert_called_once()

  def test_repair_cv_rejects_a_replacement_that_drops_facts(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      source = root / "source.html"
      source.write_text("<html><body><h1>Joseph Munemo</h1></body></html>", encoding="utf-8")

      def download(_user_id, destination, format="html"):
        Path(destination).write_text(
          "<html><body>Joseph Munemo joseph@example.com +213563719936 2024</body></html>",
          encoding="utf-8",
        )
        return Path(destination)

      args = argparse.Namespace(
        phone="+213563719936",
        source_html=str(source),
        backup_dir=str(root / "backup"),
        apply=True,
      )
      with (
        mock.patch.object(jobscout.commands_setup, "cmd_list_ready", return_value={"subscribers": [self.subscriber()]}),
        mock.patch.object(jobscout.api, "download_cv", side_effect=download),
        mock.patch.object(jobscout.api, "upload_cv_html") as upload,
      ):
        with self.assertRaisesRegex(SystemExit, "drops canonical facts"):
          jobscout.commands_setup.cmd_repair_cv(args)
      upload.assert_not_called()


if __name__ == "__main__":
  unittest.main()
