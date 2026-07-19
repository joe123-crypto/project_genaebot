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

class JSearchTests(unittest.TestCase):
  def test_search_queries_use_roles_locations_and_default_location(self) -> None:
    self.assertEqual(
      job_scout.search_queries({
        "targetRoles": ["Security guard", "Driver"],
        "locations": ["Algiers"],
        "language": "fr",
      }),
      ["Security guard à Algiers", "Driver à Algiers"],
    )
    self.assertEqual(
      job_scout.search_queries({"targetRoles": ["Waitress"], "locations": ["Harare, Zimbabwe"], "language": "en"}),
      ["Waitress in Harare, Zimbabwe"],
    )
    self.assertEqual(
      job_scout.search_queries({"targetRoles": ["Waitress"], "locations": ["Nationwide, Zimbabwe"], "language": "en"}),
      ["Waitress in Zimbabwe"],
    )
    self.assertEqual(
      job_scout.search_queries({"targetRoles": ["Cleaner"]}),
      ["Cleaner in Algeria"],
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

  def test_normalized_candidate_keeps_four_thousand_description_chars(self) -> None:
    description = "R" * 4500
    candidate = job_scout.normalized_candidate(
      {
        "job_id": "long-desc",
        "employer_name": "Example SARL",
        "job_title": "Technicien",
        "job_description": description,
      },
      "Technicien",
    )

    self.assertEqual(len(candidate["description"]), 4000)
    self.assertEqual(candidate["description"], "R" * 4000)

  def test_fetch_jsearch_candidates_uses_profile_country_and_language(self) -> None:
    # jsearch_request is called by fetch_jsearch_candidates within jobscout.jsearch,
    # so patch it at its defining module rather than the job_scout re-export.
    with mock.patch("jobscout.jsearch.jsearch_request") as request:
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

  def test_waitstaff_search_uses_one_targeted_query_and_filters_unrelated_roles(self) -> None:
    response = {"data": [
      {"job_id": "1", "employer_name": "Hotel", "job_title": "Group Executive Chef", "job_description": "Lead kitchens."},
      {"job_id": "2", "employer_name": "Cafe", "job_title": "Waiter", "job_description": "Serve diners."},
      {"job_id": "3", "employer_name": "Tech", "job_title": "Server Administrator", "job_description": "Maintain Linux servers."},
    ]}
    with mock.patch("jobscout.jsearch.jsearch_request", return_value=response) as request:
      candidates, result = job_scout.fetch_jsearch_candidates_with_result(
        {"targetRoles": ["Waitress"], "locations": ["Harare, Zimbabwe"], "country": "zw", "language": "en"},
        5,
      )

    request.assert_called_once_with("Waitress in Harare, Zimbabwe", "zw", "en", 5)
    self.assertEqual([candidate["role"] for candidate in candidates], ["Waiter"])
    self.assertEqual(result["rawCount"], 3)
    self.assertEqual(result["matchedCount"], 1)

  def test_jsearch_explicitly_expired_results_are_removed(self) -> None:
    response = {"data": [
      {"job_id": "1", "employer_name": "Cafe", "job_title": "Waiter", "job_is_expired": True},
      {"job_id": "2", "employer_name": "Hotel", "job_title": "Waitress", "job_is_expired": False},
    ]}
    with mock.patch("jobscout.jsearch.jsearch_request", return_value=response):
      candidates, result = job_scout.fetch_jsearch_candidates_with_result(
        {"targetRoles": ["Waitress"], "locations": ["Harare, Zimbabwe"], "country": "zw", "language": "en"},
        5,
      )
    self.assertEqual([candidate["role"] for candidate in candidates], ["Waitress"])
    self.assertEqual(result["expiredCount"], 1)

  def test_closing_state_and_filter_keep_today_and_unknown_but_drop_expired(self) -> None:
    now = datetime(2026, 7, 5, 12, tzinfo=timezone.utc)
    self.assertEqual(job_scout.closing_state("04 JUL 2026", now), "expired")
    self.assertEqual(job_scout.closing_state("05 JUL 2026", now), "open")
    self.assertEqual(job_scout.closing_state("2026-07-06T00:00:00Z", now), "open")
    self.assertEqual(job_scout.closing_state(None, now), "unknown")
    self.assertEqual(job_scout.closing_state("when filled", now), "malformed")

    candidates = [
      {"company": "A", "role": "Waiter", "source": "jsearch", "closing": "01 Jul 2026", "candidateId": "1"},
      {"company": "B", "role": "Waiter", "source": "jsearch", "closing": None, "candidateId": "2"},
    ]
    with mock.patch("jobscout.jsearch.closing_state", side_effect=["expired", "unknown"]):
      selected, stats = job_scout.filter_candidates_with_stats(candidates, {}, [], 5)
    self.assertEqual([item["company"] for item in selected], ["B"])
    self.assertEqual(stats["jsearch"]["expiredCount"], 1)
    self.assertEqual(stats["jsearch"]["unknownDeadlineCount"], 1)

  def test_channel_permalink_is_not_treated_as_application_url(self) -> None:
    candidate = {
      "source": "zim-job-vacancies-whatsapp",
      "sourceUrl": "https://whatsapp.com/channel/invite/123",
      "sourcePostUrl": "https://whatsapp.com/channel/invite/123",
      "sourceChannelJid": "123@newsletter",
      "sourcePostId": "123",
      "applyLinks": [],
    }
    self.assertIsNone(job_scout.candidate_application_url(candidate))
    self.assertIn("Review the original advert", job_scout.action_instruction(candidate, "channel_post_manual"))
    self.assertIn(candidate["sourcePostUrl"], job_scout.action_instruction(candidate, "channel_post_manual"))

  def test_whatsapp_channel_results_are_reported_per_source(self) -> None:
    candidates = [
      {"source": "zimlinks-whatsapp", "role": "Driver"},
      {"source": "zim-job-vacancies-whatsapp", "role": "Assistant"},
    ]
    with mock.patch("scrapers.zimlinks.fetch", return_value=candidates):
      found, results = job_scout.fetch_whatsapp_channel_candidates_with_results(
        {"targetRoles": ["Any"], "country": "zw"},
        5,
      )
    self.assertEqual(found, candidates)
    self.assertEqual(results["zimlinks-whatsapp"]["rawCount"], 1)
    self.assertEqual(results["zim-job-vacancies-whatsapp"]["rawCount"], 1)

  def test_fetch_jsearch_candidates_soft_fails_on_outage(self) -> None:
    # A transient JSearch outage must not raise: the caller falls back to scrapers.
    with mock.patch("jobscout.jsearch.jsearch_request", side_effect=job_scout.JSearchUnavailable("HTTP 504")):
      candidates = job_scout.fetch_jsearch_candidates({"targetRoles": ["Driver"], "locations": ["Harare"]}, 5)
    self.assertEqual(candidates, [])
    self.assertFalse(issubclass(job_scout.JSearchUnavailable, SystemExit))

  def test_jsearch_request_maps_read_timeout_to_unavailable(self) -> None:
    # A socket read timeout (TimeoutError, not HTTPError/URLError) must still be
    # classified transient so dispatch falls back to the scraped boards.
    with mock.patch("jobscout.jsearch.jsearch_provider", return_value="rapidapi"), \
         mock.patch("jobscout.jsearch.env_default", side_effect=lambda key, default="": "key" if "KEY" in key else default), \
         mock.patch("urllib.request.urlopen", side_effect=TimeoutError("The read operation timed out")) as request, \
         mock.patch("jobscout.jsearch.time.sleep"):
      with self.assertRaises(job_scout.JSearchUnavailable):
        job_scout.jsearch_request("driver", "dz", "fr", 5)
    self.assertEqual(request.call_count, 2)

  def test_jsearch_request_retries_one_transient_failure(self) -> None:
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = b'{"data": []}'
    with mock.patch("jobscout.jsearch.jsearch_provider", return_value="rapidapi"), \
         mock.patch("jobscout.jsearch.env_default", side_effect=lambda key, default="": "key" if "KEY" in key else default), \
         mock.patch("urllib.request.urlopen", side_effect=[urllib.error.URLError("dns"), response]) as request, \
         mock.patch("jobscout.jsearch.time.sleep") as sleep:
      result = job_scout.jsearch_request("driver", "dz", "fr", 5)
    self.assertEqual(result, {"data": []})
    self.assertEqual(request.call_count, 2)
    sleep.assert_called_once_with(job_scout.JSEARCH_RETRY_SECONDS)

  def test_jsearch_request_does_not_retry_http_400(self) -> None:
    error = urllib.error.HTTPError("https://example.test", 400, "bad", {}, io.BytesIO(b"bad request"))
    with mock.patch("jobscout.jsearch.jsearch_provider", return_value="rapidapi"), \
         mock.patch("jobscout.jsearch.env_default", side_effect=lambda key, default="": "key" if "KEY" in key else default), \
         mock.patch("urllib.request.urlopen", side_effect=error) as request, \
         mock.patch("jobscout.jsearch.time.sleep") as sleep:
      with self.assertRaisesRegex(job_scout.JSearchUnavailable, "HTTP 400"):
        job_scout.jsearch_request("driver", "dz", "fr", 5)
    request.assert_called_once()
    sleep.assert_not_called()

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

    self.assertEqual([item["company"] for item in filtered], ["C"])

  def test_filter_retries_failed_but_not_historical_manual_records(self) -> None:
    candidates = [
      {"company": "A", "role": "Driver", "sourceUrl": "https://a.test", "candidateId": "1"},
      {"company": "B", "role": "Clerk", "sourceUrl": "https://b.test", "candidateId": "2"},
    ]
    applications = [
      {"company": "A", "role": "Driver", "status": "failed", "attemptCount": 1},
      {"company": "B", "role": "Clerk", "status": "physical_submission"},
    ]

    self.assertEqual([item["company"] for item in job_scout.filter_candidates(candidates, {}, applications, 5)], ["A"])

  def test_global_filter_drops_expired_and_keeps_email_duplicate_variant(self) -> None:
    candidates = [
      {
        "company": "Acme",
        "role": "Driver",
        "source": "jsearch",
        "sourceUrl": "https://jobs.example/acme-driver",
        "candidateId": "a",
        "closing": "01 Jul 2026",
      },
      {
        "company": "Beta",
        "role": "Clerk",
        "source": "jsearch",
        "sourceUrl": "https://jobs.example/beta-clerk",
        "candidateId": "b",
        "applicationEmail": None,
      },
      {
        "company": "Beta",
        "role": "Clerk",
        "source": "vacancymail",
        "sourceUrl": "https://board.example/beta-clerk",
        "candidateId": "c",
        "applicationEmail": "hr@beta.example",
      },
    ]

    cleaned, stats, by_key = job_scout.global_filter_candidates(candidates)

    self.assertEqual([item["company"] for item in cleaned], ["Beta"])
    self.assertEqual(cleaned[0]["applicationEmail"], "hr@beta.example")
    self.assertEqual(stats["jsearch"]["expiredCount"], 1)
    self.assertEqual(stats["jsearch"]["duplicateCount"], 1)
    self.assertIn(job_scout.duplicate_key(candidates[2]), by_key)

  def test_filter_ranks_email_candidates_before_earlier_manual_candidates(self) -> None:
    candidates = [
      {
        "company": "Manual Co",
        "role": "Driver",
        "source": "jsearch",
        "sourceUrl": "https://manual.example/job",
        "candidateId": "manual",
      },
      {
        "company": "Email Co",
        "role": "Assistant",
        "source": "vacancymail",
        "sourceUrl": "https://email.example/job",
        "candidateId": "email",
        "applicationEmail": "jobs@email.example",
      },
    ]

    selected = job_scout.filter_candidates(candidates, {"targetRoles": ["Driver"]}, [], 1)

    self.assertEqual(selected[0]["company"], "Email Co")
    self.assertEqual(selected[0]["applicationPriority"], "email")
    self.assertEqual(selected[0]["selectionRank"], 1)

  def test_filter_ranks_stronger_profile_fit_within_same_application_priority(self) -> None:
    candidates = [
      {
        "company": "Weak Co",
        "role": "Assistant",
        "description": "General office work.",
        "source": "jsearch",
        "candidateId": "weak",
        "applicationEmail": "jobs@weak.example",
      },
      {
        "company": "Strong Co",
        "role": "IT Support Technician",
        "description": "Computer literacy, Microsoft Office, and troubleshooting.",
        "source": "jsearch",
        "candidateId": "strong",
        "applicationEmail": "jobs@strong.example",
      },
    ]
    preferences = {
      "targetRoles": ["IT Support"],
      "skills": ["Computer literacy", "Microsoft Office"],
    }

    selected = job_scout.filter_candidates(candidates, preferences, [], 1)

    self.assertEqual(selected[0]["company"], "Strong Co")
    self.assertGreater(selected[0]["selectionScore"], 0)

  def test_jsearch_default_window_is_one_month(self) -> None:
    self.assertEqual(job_scout.DEFAULT_JSEARCH_DATE_POSTED, "month")

if __name__ == "__main__":
  unittest.main()
