#!/usr/bin/env python3

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
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

# Always in the future so the cache expiry filter never drops the fixture.
FUTURE_CLOSING = (date.today() + timedelta(days=28)).strftime("%-d %B %Y")


class GroupFeedTests(unittest.TestCase):
  def candidate(self, **overrides) -> dict:
    data = {
      "candidateId": "job-1",
      "source": "vacancymail",
      "sourceUrl": "https://vacancymail.co.zw/jobs/poultry-supervisor/",
      "applyLinks": ["https://vacancymail.co.zw/jobs/poultry-supervisor/"],
      "applicationEmails": ["learnmore@tirufresh.co.zw"],
      "applicationEmail": "learnmore@tirufresh.co.zw",
      "company": "Tiru Fresh",
      "role": "Poultry Supervisor",
      "location": "Lot 4 Pollockshields Estate, Selous",
      "closing": FUTURE_CLOSING,
      "description": (
        "About Us We are a leading commercial poultry farming company. "
        "Key Responsibilities • Supervise daily poultry house operations. "
        "Application Interested candidates should submit their CV."
      ),
    }
    data.update(overrides)
    return data

  def test_group_feed_message_uses_requested_advert_shape(self) -> None:
    message = jobscout.group_feed.format_group_feed_message(self.candidate(
      applyLinks=["https://tirufresh.co.zw/apply/poultry-supervisor"],
    ))

    self.assertTrue(message.startswith("Job Title: Poultry Supervisor\n\nLocation: Lot 4 Pollockshields Estate, Selous"))
    self.assertIn("About Us", message)
    self.assertIn("Key Responsibilities", message)
    self.assertIn("- Supervise daily poultry house operations.", message)
    self.assertIn(f"Closing Date: {FUTURE_CLOSING}", message)
    self.assertIn("Email: learnmore@tirufresh.co.zw", message)
    self.assertIn("Apply: https://tirufresh.co.zw/apply/poultry-supervisor", message)
    self.assertNotIn("Source:", message)
    self.assertNotIn("https://vacancymail.co.zw/jobs/poultry-supervisor/", message)
    self.assertIn(f"Join the group for more jobs: {jobscout.constants.DEFAULT_GROUP_FEED_INVITE_URL}", message)

  def test_group_feed_message_strips_web_internal_fragments(self) -> None:
    message = jobscout.group_feed.format_group_feed_message(self.candidate(
      description=(
        "About Us\n"
        "We are hiring for a public-facing role.\n"
        "<script>window.adsbygoogle = window.adsbygoogle || [];</script>\n"
        "(adsbygoogle = window.adsbygoogle || []).push({});\n"
        '{"@context":"https://schema.org","@type":"JobPosting"}\n'
        ".entry-content{display:block;color:#111;}\n"
        "Key Responsibilities\n"
        "- Drive safely across assigned routes.\n"
      ),
    ))

    self.assertIn("We are hiring for a public-facing role.", message)
    self.assertIn("- Drive safely across assigned routes.", message)
    self.assertNotIn("adsbygoogle", message)
    self.assertNotIn("window.", message)
    self.assertNotIn("@context", message)
    self.assertNotIn("schema.org", message)
    self.assertNotIn("entry-content", message)
    self.assertTrue(message.endswith(jobscout.constants.DEFAULT_GROUP_FEED_INVITE_URL))

  def test_group_feed_message_keeps_prose_that_resembles_code(self) -> None:
    message = jobscout.group_feed.format_group_feed_message(self.candidate(
      description=(
        "You will own the finance function (reporting to the CFO).\n"
        "Deliverables => weekly reports and monthly reconciliations.\n"
        "const tracker = window.adsbygoogle || [];\n"
      ),
    ))

    self.assertIn("You will own the finance function (reporting to the CFO).", message)
    self.assertIn("Deliverables => weekly reports and monthly reconciliations.", message)
    self.assertNotIn("window.adsbygoogle", message)

  def test_group_feed_message_uses_clean_short_title(self) -> None:
    message = jobscout.group_feed.format_group_feed_message(self.candidate(
      role="motivated, detail-oriented, and enthusiastic Accounting Intern to join our Finance Department",
      description="Application Send your CV to hr@example.co.zw.",
    ))
    self.assertTrue(message.startswith("Job Title: Unknown role"))

    doctor = jobscout.group_feed.format_group_feed_message(self.candidate(
      role="Doctor* to attend to patients in accordance with regulations.",
    ))
    self.assertTrue(doctor.startswith("Job Title: Doctor to attend to patients"))

    assistant = jobscout.group_feed.format_group_feed_message(self.candidate(role="Assistant to the CEO"))
    self.assertTrue(assistant.startswith("Job Title: Assistant to the CEO"))

    officer = jobscout.group_feed.format_group_feed_message(self.candidate(role="Officer for Finance"))
    self.assertTrue(officer.startswith("Job Title: Officer for Finance"))

    hiring = jobscout.group_feed.format_group_feed_message(self.candidate(
      role="Cimas Health Group is hiring: Internal Auditor | Closing Date: 10 July 2026",
    ))
    self.assertTrue(hiring.startswith("Job Title: Internal Auditor"))

  def test_group_feed_cache_dedupes_and_writes_latest(self) -> None:
    candidate = self.candidate()
    duplicate = {**candidate, "candidateId": "job-duplicate"}
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      with mock.patch(
        "jobscout.group_feed.fetch_whatsapp_channel_candidates_with_results",
        return_value=([candidate], {"zimlinks-whatsapp": {"status": "ok", "rawCount": 1, "durationMs": 1}}),
      ), mock.patch(
        "jobscout.group_feed.fetch_scraped_candidates_with_results",
        return_value=([duplicate], {"vacancymail": {"status": "ok", "rawCount": 1, "durationMs": 1}}),
      ):
        result = jobscout.group_feed.build_group_feed_cache("run-1", root=root, limit=10)

      self.assertTrue(result["ok"])
      cache = json.loads((root / "latest.json").read_text(encoding="utf-8"))
      self.assertEqual(cache["candidateCount"], 1)
      self.assertEqual(cache["candidates"][0]["role"], "Poultry Supervisor")

  def test_group_feed_cache_skips_low_information_candidates(self) -> None:
    low_info = self.candidate(
      candidateId="low-info",
      source="everyday-vacancies-whatsapp",
      sourceUrl="https://whatsapp.com/channel/example/low-info",
      sourcePostUrl="https://whatsapp.com/channel/example/low-info",
      applyLinks=[],
      contactLinks=[],
      applicationEmails=[],
      applicationEmail=None,
      company="Unknown employer",
      role="Sales and Commercial Officer",
      location="",
      closing=None,
      description="Sales and Commercial Officer Wanted",
    )
    actionable_short = self.candidate(
      candidateId="actionable-short",
      source="everyday-vacancies-whatsapp",
      sourceUrl="https://whatsapp.com/channel/example/actionable-short",
      sourcePostUrl="https://whatsapp.com/channel/example/actionable-short",
      applyLinks=[],
      contactLinks=["https://wa.me/263700000001"],
      applicationEmails=[],
      applicationEmail=None,
      company="Unknown employer",
      role="Receptionist",
      location="",
      closing=None,
      description="Receptionist Vacancy",
    )
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      with mock.patch(
        "jobscout.group_feed.fetch_whatsapp_channel_candidates_with_results",
        return_value=([low_info, actionable_short], {"everyday-vacancies-whatsapp": {"status": "ok", "rawCount": 2, "durationMs": 1}}),
      ), mock.patch(
        "jobscout.group_feed.fetch_scraped_candidates_with_results",
        return_value=([], {"vacancymail": {"status": "ok", "rawCount": 0, "durationMs": 1}}),
      ):
        result = jobscout.group_feed.build_group_feed_cache("run-low-info", root=root, limit=10)

      cache = json.loads((root / "latest.json").read_text(encoding="utf-8"))
      self.assertTrue(result["ok"])
      self.assertEqual(cache["candidateCount"], 1)
      self.assertEqual(cache["candidates"][0]["candidateId"], "actionable-short")
      self.assertEqual(result["sourceResults"]["_filter"]["lowInfoCount"], 1)

  def test_deliver_group_feed_reads_cache_and_marks_success_only(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      candidate = self.candidate()
      jobscout.group_feed.write_group_feed_cache(
        "cache-run",
        [{**candidate, "groupFeedKey": jobscout.group_feed.candidate_delivery_key(candidate)}],
        {},
        root,
      )
      args = argparse.Namespace(
        target="120363000000000000@g.us",
        invite_url="https://chat.whatsapp.com/example",
        group_feed_root=root,
        cache_max_age_seconds=3600,
        max_messages=0,
        dry_run=False,
      )
      with mock.patch("jobscout.group_feed.fetch_whatsapp_channel_candidates_with_results") as channel_fetch, \
           mock.patch("jobscout.group_feed.fetch_scraped_candidates_with_results") as board_fetch, \
           mock.patch.object(jobscout.commands_group_feed, "send_whatsapp_message", return_value={"messageId": "wa-1"}) as send:
        result = job_scout.cmd_deliver_group_feed(args)

      self.assertTrue(result["ok"])
      self.assertEqual(result["sentCount"], 1)
      send.assert_called_once()
      self.assertIn("Join the group for more jobs: https://chat.whatsapp.com/example", send.call_args.args[1])
      channel_fetch.assert_not_called()
      board_fetch.assert_not_called()
      state = json.loads((root / "delivered.json").read_text(encoding="utf-8"))
      self.assertEqual(len(state["delivered"]), 1)

      with mock.patch.object(jobscout.commands_group_feed, "send_whatsapp_message") as send_again:
        second = job_scout.cmd_deliver_group_feed(args)
      self.assertEqual(second["sentCount"], 0)
      self.assertEqual(second["skippedCount"], 1)
      send_again.assert_not_called()

  def test_deliver_group_feed_does_not_mark_failed_send(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      candidate = self.candidate()
      jobscout.group_feed.write_group_feed_cache("cache-run", [candidate], {}, root)
      args = argparse.Namespace(
        target="120363000000000000@g.us",
        invite_url="https://chat.whatsapp.com/example",
        group_feed_root=root,
        cache_max_age_seconds=3600,
        max_messages=0,
        dry_run=False,
      )
      with mock.patch.object(jobscout.commands_group_feed, "send_whatsapp_message", side_effect=RuntimeError("send failed")):
        result = job_scout.cmd_deliver_group_feed(args)

      self.assertFalse(result["ok"])
      self.assertEqual(result["failedCount"], 1)
      self.assertFalse((root / "delivered.json").exists())

  def test_deliver_group_feed_caps_and_spaces_sends(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      candidates = [
        self.candidate(
          candidateId=f"job-{index}",
          sourceUrl=f"https://vacancymail.co.zw/jobs/role-{index}/",
        )
        for index in range(20)
      ]
      jobscout.group_feed.write_group_feed_cache("cache-run", candidates, {}, root)
      args = argparse.Namespace(
        target="120363000000000000@g.us",
        invite_url="https://chat.whatsapp.com/example",
        group_feed_root=root,
        cache_max_age_seconds=3600,
        max_messages=jobscout.constants.DEFAULT_GROUP_FEED_MAX_MESSAGES,
        send_delay_seconds=5,
        dry_run=False,
      )
      with mock.patch.object(jobscout.commands_group_feed, "send_whatsapp_message", return_value={"messageId": "wa"}) as send, \
           mock.patch.object(jobscout.commands_group_feed.time, "sleep") as sleep:
        result = job_scout.cmd_deliver_group_feed(args)

      self.assertTrue(result["ok"])
      self.assertEqual(result["sentCount"], jobscout.constants.DEFAULT_GROUP_FEED_MAX_MESSAGES)
      self.assertEqual(send.call_count, jobscout.constants.DEFAULT_GROUP_FEED_MAX_MESSAGES)
      # A pause sits between sends, never before the first one.
      self.assertEqual(sleep.call_count, jobscout.constants.DEFAULT_GROUP_FEED_MAX_MESSAGES - 1)
      sleep.assert_called_with(5.0)

  def test_deliver_group_feed_default_cap_is_applied_via_parser(self) -> None:
    parser = job_scout.build_parser()
    args = parser.parse_args(["deliver-group-feed", "--target", "120363000000000000@g.us"])
    self.assertEqual(args.max_messages, jobscout.constants.DEFAULT_GROUP_FEED_MAX_MESSAGES)
    self.assertEqual(args.send_delay_seconds, jobscout.constants.DEFAULT_GROUP_FEED_SEND_DELAY_SECONDS)
    self.assertIsNone(args.invite_url)

  def test_deliver_group_feed_invite_url_env_is_used_when_flag_omitted(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      candidate = self.candidate()
      jobscout.group_feed.write_group_feed_cache("cache-run", [candidate], {}, root)
      parser = job_scout.build_parser()
      args = parser.parse_args([
        "deliver-group-feed",
        "--target",
        "120363000000000000@g.us",
        "--group-feed-root",
        str(root),
        "--max-messages",
        "1",
        "--dry-run",
      ])
      with mock.patch.dict(job_scout.os.environ, {"JOB_SCOUT_GROUP_FEED_INVITE_URL": "https://chat.whatsapp.com/CUSTOM_FROM_ENV"}):
        result = job_scout.cmd_deliver_group_feed(args)

      self.assertTrue(result["ok"])
      self.assertIn("Join the group for more jobs: https://chat.whatsapp.com/CUSTOM_FROM_ENV", result["messages"][0]["message"])

  def test_dispatch_cache_write_is_opt_in(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      args = argparse.Namespace(
        run_id="unit-dispatch",
        phone=None,
        limit_ready=10,
        max_candidates_per_user=5,
        dispatch_root=Path(tmp),
        dry_run=True,
        browser_apply=False,
        write_group_feed_cache=True,
        group_feed_root=Path(tmp) / "group-feed",
        group_feed_limit=10,
      )
      with mock.patch.object(jobscout.dispatch_runner, "cmd_list_ready", return_value={"subscribers": []}), \
           mock.patch("jobscout.group_feed.fetch_whatsapp_channel_candidates_with_results", return_value=([], {})) as channel_fetch, \
           mock.patch("jobscout.group_feed.fetch_scraped_candidates_with_results", return_value=([], {})):
        result = job_scout.cmd_dispatch_jsearch(args)

    self.assertTrue(result["groupFeedCache"]["dryRun"])
    channel_fetch.assert_called_once()

if __name__ == "__main__":
  unittest.main()
