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

class JSearchDispatchTests(unittest.TestCase):
  def subscriber(self, auto_apply: bool = True) -> dict:
    return {
      "userId": "user-1",
      "whatsappPhone": "+213555010101",
      "whatsappPhoneHash": "phonehash1",
      "senderEmail": "applicant@example.com",
      "gmailConnected": True,
      "ready": True,
      "cvFileRef": "user-1/cv/base/cv.html",
      "cvHtmlRef": "user-1/cv/base/cv.html",
      "cvConversionStatus": "ready",
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

  def args(self, root: Path, dry_run: bool = False, browser_apply: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
      run_id="unit-dispatch",
      phone=None,
      user_id=None,
      limit_ready=10,
      max_candidates_per_user=5,
      dispatch_root=root,
      dry_run=dry_run,
      browser_apply=browser_apply,
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
    browser_apply: bool = False,
    send_error: Exception | None = None,
    web_result: dict | None = None,
    max_candidates_per_user: int = 5,
    gmail_connected: bool = True,
  ) -> dict:
    subscriber = self.subscriber(auto_apply=auto_apply)
    with (
      mock.patch.object(jobscout.dispatch_runner, "cmd_list_ready", return_value={"subscribers": [subscriber]}),
      mock.patch.object(jobscout.dispatch_runner, "cmd_list_applications", return_value={"applications": []}),
      mock.patch(
        "jobscout.jsearch.fetch_jsearch_candidates_with_result",
        return_value=(candidates, {"status": "ok", "rawCount": len(candidates), "durationMs": 1}),
      ),
      mock.patch.object(jobscout.dispatch_runner, "jsearch_provider", return_value="rapidapi"),
      mock.patch.object(jobscout.dispatch_runner, "write_cover_letter", return_value=root / "cover-letter.txt"),
      mock.patch.object(jobscout.dispatch_runner, "web_apply_payload", return_value={"applicationUrl": "https://jobs.example/apply"}),
      mock.patch.object(
        jobscout.dispatch_runner,
        "request_web_application",
        return_value=web_result or {"outcome": "action_required", "blockerCode": "login_required"},
      ) as web_apply,
      mock.patch.object(jobscout.dispatch_runner, "send_whatsapp_summary", return_value={"ok": True}) as whatsapp,
      mock.patch.object(jobscout.dispatch_runner, "cmd_record_application", return_value={"ok": True}) as record,
      mock.patch.object(jobscout.dispatch_runner, "gmail_sender_connected", return_value=gmail_connected) as gmail_check,
      mock.patch.object(jobscout.dispatch_runner, "publish_task_status", return_value=True) as telemetry,
    ):
      if send_error is None:
        send_patch = mock.patch.object(jobscout.dispatch_runner, "send_application_email", return_value={"messageId": "msg-1"})
      else:
        send_patch = mock.patch.object(jobscout.dispatch_runner, "send_application_email", side_effect=send_error)
      with send_patch as send:
        args = self.args(root, browser_apply=browser_apply)
        args.max_candidates_per_user = max_candidates_per_user
        result = job_scout.cmd_dispatch_jsearch(args)
    result["_record_calls"] = record.call_args_list
    result["_send_calls"] = send.call_args_list
    result["_whatsapp_calls"] = whatsapp.call_args_list
    result["_web_calls"] = web_apply.call_args_list
    result["_gmail_check_calls"] = gmail_check.call_args_list
    result["_telemetry_calls"] = {call.kwargs["task_id"]: call.kwargs for call in telemetry.call_args_list}
    return result

  def test_dispatch_applies_email_candidate_and_records_outcome(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      result = self.dispatch_with(Path(tmp), [self.candidate()])

      self.assertEqual(result["appliedCount"], 1)
      self.assertEqual(result["physicalCount"], 0)
      self.assertEqual(result["failedCount"], 0)
      self.assertEqual(result["users"][0]["status"], "applied")
      self.assertEqual(result["_send_calls"][0].args[1]["applicationEmail"], "hr@example.dz")
      self.assertEqual(len(result["_gmail_check_calls"]), 1)
      record_args = result["_record_calls"][0].args[0]
      self.assertEqual(record_args.status, "applied")
      self.assertEqual(record_args.source, "jsearch")

  def test_dispatch_records_action_required_when_gmail_sender_missing(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      result = self.dispatch_with(Path(tmp), [self.candidate()], gmail_connected=False)

      self.assertEqual(result["appliedCount"], 0)
      self.assertEqual(result["actionRequiredCount"], 1)
      self.assertEqual(result["failedCount"], 0)
      self.assertEqual(result["users"][0]["status"], "action_required")
      self.assertEqual(result["_send_calls"], [])
      record_args = result["_record_calls"][0].args[0]
      self.assertEqual(record_args.status, "action_required")
      self.assertEqual(record_args.submission_method, "manual")
      self.assertEqual(record_args.blocker_code, "gmail_not_connected")
      public_message = result["_whatsapp_calls"][0].args[1]
      self.assertIn("Reconnect Gmail", public_message)

  def test_dispatch_applies_top_ranked_candidates_within_user_limit(self) -> None:
    candidates = [
      {
        "candidateId": "manual",
        "source": "jsearch",
        "sourceUrl": "https://jobs.example/manual",
        "applyLinks": ["https://jobs.example/manual"],
        "company": "Manual Logistics",
        "role": "Driver",
        "location": "Algiers, DZ",
        "query": "Driver a Algiers",
        "closing": None,
        "description": "Delivery role.",
      },
      {
        "candidateId": "weak",
        "source": "jsearch",
        "sourceUrl": "https://jobs.example/weak",
        "applyLinks": ["https://jobs.example/weak"],
        "applicationEmails": ["weak@example.dz"],
        "applicationEmail": "weak@example.dz",
        "company": "Weak Logistics",
        "role": "Assistant",
        "location": "Algiers, DZ",
        "query": "Driver a Algiers",
        "closing": None,
        "description": "General office work.",
      },
      {
        "candidateId": "strong",
        "source": "jsearch",
        "sourceUrl": "https://jobs.example/strong",
        "applyLinks": ["https://jobs.example/strong"],
        "applicationEmails": ["strong@example.dz"],
        "applicationEmail": "strong@example.dz",
        "company": "Strong Logistics",
        "role": "Driver",
        "location": "Algiers, DZ",
        "query": "Driver a Algiers",
        "closing": None,
        "description": "Delivery driver role.",
      },
    ]
    with tempfile.TemporaryDirectory() as tmp:
      result = self.dispatch_with(Path(tmp), candidates, max_candidates_per_user=2)

      self.assertEqual(result["appliedCount"], 2)
      self.assertEqual([call.args[1]["company"] for call in result["_send_calls"]], ["Strong Logistics", "Weak Logistics"])
      self.assertEqual(result["users"][0]["candidateCount"], 2)

  def test_dispatch_phone_scope_selects_only_requested_subscriber(self) -> None:
    requested = self.subscriber()
    other = {
      **self.subscriber(),
      "userId": "user-2",
      "whatsappPhone": "+213555020202",
      "whatsappPhoneHash": "phonehash2",
    }
    with tempfile.TemporaryDirectory() as tmp:
      args = self.args(Path(tmp), dry_run=True)
      args.phone = "+213 555 010 101"
      with mock.patch.object(jobscout.dispatch_runner, "cmd_list_ready", return_value={"subscribers": [other, requested]}), \
           mock.patch.object(jobscout.dispatch_runner, "cmd_list_applications", return_value={"applications": []}) as applications, \
           mock.patch("jobscout.jsearch.fetch_jsearch_candidates_with_result", return_value=([], {"status": "ok", "rawCount": 0, "durationMs": 1})), \
           mock.patch("jobscout.jsearch.fetch_scraped_candidates_with_results", return_value=([], {name: {"status": "skipped", "rawCount": 0, "durationMs": 0} for name in ("vacancymail", "iharare", "vacancybox", "applynow")})), \
           mock.patch.object(jobscout.dispatch_runner, "jsearch_provider", return_value="test"):
        result = job_scout.cmd_dispatch_jsearch(args)

    self.assertTrue(result["scoped"])
    self.assertEqual(result["readyCount"], 1)
    self.assertEqual(result["processedCount"], 1)
    applications.assert_called_once()
    self.assertEqual(applications.call_args.args[0].user_id, "user-1")

  def test_dispatch_phone_scope_rejects_non_ready_subscriber(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      args = self.args(Path(tmp), dry_run=True)
      args.phone = "+213555099999"
      with mock.patch.object(jobscout.dispatch_runner, "cmd_list_ready", return_value={"subscribers": [self.subscriber()]}):
        with self.assertRaisesRegex(SystemExit, "no ready Job Scout subscriber"):
          job_scout.cmd_dispatch_jsearch(args)

  def test_dispatch_user_id_scope_selects_web_only_subscriber(self) -> None:
    requested = {
      **self.subscriber(),
      "userId": "web-user-1",
      "whatsappPhone": None,
      "whatsappPhoneHash": None,
    }
    other = {**self.subscriber(), "userId": "user-2"}
    selected = jobscout.dispatch_runner.scoped_subscribers(
      [other, requested],
      user_id="web-user-1",
    )

    self.assertEqual(selected, [requested])

  def test_web_only_subscriber_is_ready_and_keeps_email_candidates_only(self) -> None:
    subscriber = {
      **self.subscriber(),
      "whatsappPhone": None,
      "whatsappPhoneHash": None,
    }
    email_candidate = self.candidate()
    website_candidate = self.candidate(email=None)

    self.assertTrue(jobscout.dispatch_runner.subscriber_is_dispatch_ready(subscriber))
    self.assertEqual(
      jobscout.dispatch_runner.candidates_for_subscriber_channels(
        subscriber,
        [website_candidate, email_candidate],
      ),
      [email_candidate],
    )
    self.assertEqual(
      jobscout.subscriber.subscriber_phone_hash(subscriber),
      job_scout.user_key("user-1"),
    )

  def test_dispatch_filters_incomplete_subscribers_from_backend(self) -> None:
    incomplete = {
      **self.subscriber(),
      "ready": False,
      "preferences": {"targetRoles": [], "locations": []},
    }
    with tempfile.TemporaryDirectory() as tmp:
      args = self.args(Path(tmp), dry_run=True)
      with mock.patch.object(jobscout.dispatch_runner, "cmd_list_ready", return_value={"subscribers": [incomplete]}), \
           mock.patch.object(jobscout.dispatch_runner, "send_whatsapp_summary") as whatsapp:
        result = job_scout.cmd_dispatch_jsearch(args)

    self.assertEqual(result["readyCount"], 0)
    self.assertEqual(result["processedCount"], 0)
    whatsapp.assert_not_called()

  def test_dispatch_excludes_legacy_pdf_only_subscriber(self) -> None:
    legacy = {**self.subscriber(), "cvFileRef": "user-1/cv/cv.pdf", "cvHtmlRef": None, "cvConversionStatus": ""}
    self.assertTrue(jobscout.dispatch_runner.subscriber_is_dispatch_ready(self.subscriber()))
    self.assertFalse(jobscout.dispatch_runner.subscriber_is_dispatch_ready(legacy))

  def test_orphaned_active_agent_marker_no_longer_blocks_dispatch(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      jobscout.dispatch_store.set_active_agent_dispatch(root, {
        "runId": "ghost-run",
        "startedAt": jobscout.dispatch_store.utc_now(),
        "mode": "openclaw-agent",
      })
      jobscout.dispatch_store.assert_no_active_agent_dispatch(root)

      self.assertIsNone(jobscout.dispatch_store.active_agent_dispatch(root))

  def test_dispatch_lock_rejects_overlapping_run(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp) / "dispatch-runs"
      with jobscout.dispatch_store.exclusive_dispatch_lock(root, "first-run"):
        with self.assertRaisesRegex(SystemExit, "another Job Scout dispatch"):
          job_scout.cmd_dispatch_jsearch(self.args(root, dry_run=True))

  def test_dispatch_lock_stays_inside_dispatch_root(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp) / "dispatch-runs"
      with jobscout.dispatch_store.exclusive_dispatch_lock(root, "unit-run"):
        self.assertEqual(jobscout.dispatch_store.dispatch_lock_path(root), root / "dispatch.lock")
        self.assertTrue((root / "dispatch.lock").is_file())

  def test_dispatch_records_manual_action_required_when_browser_apply_disabled(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      result = self.dispatch_with(Path(tmp), [self.candidate(email=None)])

      self.assertEqual(result["appliedCount"], 0)
      self.assertEqual(result["actionRequiredCount"], 1)
      self.assertEqual(result["physicalCount"], 0)
      self.assertEqual(result["users"][0]["status"], "action_required")
      self.assertEqual(result["_web_calls"], [])
      record_args = result["_record_calls"][0].args[0]
      self.assertEqual(record_args.status, "action_required")
      self.assertEqual(record_args.submission_method, "manual")
      self.assertEqual(record_args.blocker_code, "auto_apply_disabled")
      public_message = result["_whatsapp_calls"][0].args[1]
      self.assertIn("ACTION NEEDED", public_message)
      self.assertIn("Apply manually here: https://jobs.example/apply", public_message)

  def test_dispatch_records_action_required_when_browser_site_needs_login(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      result = self.dispatch_with(Path(tmp), [self.candidate(email=None)], browser_apply=True)

      self.assertEqual(result["appliedCount"], 0)
      self.assertEqual(result["actionRequiredCount"], 1)
      self.assertEqual(result["physicalCount"], 0)
      self.assertEqual(result["users"][0]["status"], "action_required")
      self.assertEqual(len(result["_web_calls"]), 1)
      record_args = result["_record_calls"][0].args[0]
      self.assertEqual(record_args.status, "action_required")
      self.assertEqual(record_args.submission_method, "website")
      self.assertEqual(record_args.blocker_code, "login_required")

  def test_dispatch_submits_website_candidate_without_email(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      result = self.dispatch_with(
        Path(tmp),
        [self.candidate(email=None)],
        browser_apply=True,
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
      self.assertEqual(result["users"][0]["sourceResults"]["jsearch"]["status"], "ok")
      self.assertEqual(result["_record_calls"], [])
      artifact = json.loads(Path(result["users"][0]["artifactPath"]).read_text(encoding="utf-8"))
      self.assertEqual(artifact["schemaVersion"], 2)
      self.assertIn("sourceResults", artifact)

  def test_dispatch_publishes_dashboard_telemetry_after_applied_run(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      result = self.dispatch_with(Path(tmp), [self.candidate()])

      jobs_task = result["_telemetry_calls"]["search_apply_jobs"]
      self.assertEqual(jobs_task["user_id"], "user-1")
      self.assertEqual(jobs_task["service"], "job_scout")
      self.assertEqual(jobs_task["status"], "active")
      self.assertEqual(jobs_task["last_run_status"], "success")
      self.assertEqual(jobs_task["last_run_summary"], "1 application(s) sent")
      self.assertEqual(jobs_task["timezone_name"], "Africa/Harare")
      self.assertEqual(jobs_task["schedule_label"], "Daily - 8:00 AM")
      self.assertTrue(jobs_task["next_run_at"].endswith("Z"))
      delivery_task = result["_telemetry_calls"]["deliver_results"]
      self.assertEqual(delivery_task["service"], "delivery")
      self.assertEqual(delivery_task["last_run_status"], "success")
      self.assertEqual(delivery_task["last_run_summary"], "Job Scout summary delivered")

  def test_dispatch_publishes_dashboard_telemetry_for_no_matches(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      result = self.dispatch_with(Path(tmp), [])

      jobs_task = result["_telemetry_calls"]["search_apply_jobs"]
      self.assertEqual(jobs_task["last_run_status"], "success")
      self.assertEqual(jobs_task["last_run_summary"], "No new matching jobs")

  def test_dispatch_publishes_dashboard_telemetry_for_failed_run(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      result = self.dispatch_with(Path(tmp), [self.candidate()], send_error=RuntimeError("mail bridge down"))

      jobs_task = result["_telemetry_calls"]["search_apply_jobs"]
      self.assertEqual(jobs_task["last_run_status"], "failed")
      self.assertTrue(jobs_task["last_run_summary"])
      delivery_task = result["_telemetry_calls"]["deliver_results"]
      self.assertEqual(delivery_task["last_run_status"], "success")

  def test_dispatch_dry_run_publishes_no_dashboard_telemetry(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      args = self.args(Path(tmp), dry_run=True)
      with mock.patch.object(jobscout.dispatch_runner, "cmd_list_ready", return_value={"subscribers": [self.subscriber()]}), \
           mock.patch.object(jobscout.dispatch_runner, "cmd_list_applications", return_value={"applications": []}), \
           mock.patch("jobscout.jsearch.fetch_jsearch_candidates_with_result", return_value=([], {"status": "ok", "rawCount": 0, "durationMs": 1})), \
           mock.patch("jobscout.jsearch.fetch_scraped_candidates_with_results", return_value=([], {name: {"status": "skipped", "rawCount": 0, "durationMs": 0} for name in ("vacancymail", "iharare", "vacancybox", "applynow")})), \
           mock.patch.object(jobscout.dispatch_runner, "jsearch_provider", return_value="test"), \
           mock.patch.object(jobscout.dispatch_runner, "publish_task_status") as telemetry:
        job_scout.cmd_dispatch_jsearch(args)

    telemetry.assert_not_called()

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
    candidate["role"] = "Cimas Health Group is hiring: Internal Auditor — Risk | Closing Date: 10 July 2026"
    with mock.patch("jobscout.delivery.run_json_command", return_value={"messageId": "msg-1"}) as run:
      result = job_scout.send_application_email(
        subscriber,
        candidate,
        Path("/tmp/cover-letter.txt"),
        cv_pdf=Path("/tmp/cv.pdf"),
        cover_letter_pdf=Path("/tmp/cover-letter.pdf"),
      )

    self.assertEqual(result["messageId"], "msg-1")
    command = run.call_args.args[0]
    self.assertIn("--from", command)
    self.assertIn("applicant@example.com", command)
    self.assertIn("--sender-uid", command)
    self.assertEqual(command[command.index("--sender-uid") + 1], "user-1")
    self.assertIn("--subject", command)
    subject = command[command.index("--subject") + 1]
    self.assertEqual(subject, "Application for Internal Auditor - Risk - Test Applicant")
    self.assertNotIn("—", subject)
    self.assertNotIn("–", subject)

  def test_gmail_sender_connected_checks_sender_registry_key(self) -> None:
    subscriber = self.subscriber()
    sender_key = jobscout.delivery.token_store_key_for_uid("user-1")
    with mock.patch("jobscout.delivery.run_json_command", return_value={"senders": {sender_key: ["central"]}}) as run:
      self.assertTrue(job_scout.gmail_sender_connected(subscriber))

    self.assertEqual(run.call_args.args[0], [str(job_scout.GENAIE_MAIL_BIN), "senders"])

  def test_send_whatsapp_summary_uses_gateway_state_and_token(self) -> None:
    with mock.patch.dict(job_scout.os.environ, {
      "OPENCLAW_HOME": "/home/joseph/.openclaw",
      "GATEWAY_AUTH_TOKEN": "gateway-token",
      "KEEP_ME": "yes",
    }, clear=True), mock.patch(
      "jobscout.delivery.run_json_command",
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
    board_markers = ["vacancymail", "iharare", "vacancybox", "applynow"]
    with tempfile.TemporaryDirectory() as tmp:
      with mock.patch("scrapers.fetch_scraped_candidates_with_results") as inner:
        result = self.dispatch_with(Path(tmp), [self.candidate()])
      # The dz subscriber is gated out, so the scraper package is never invoked.
      inner.assert_not_called()

    for marker in board_markers:
      self.assertEqual(result["users"][0]["sourceResults"][marker]["status"], "skipped")
      self.assertEqual(result["users"][0]["sourceResults"][marker]["rawCount"], 0)

if __name__ == "__main__":
  unittest.main()
