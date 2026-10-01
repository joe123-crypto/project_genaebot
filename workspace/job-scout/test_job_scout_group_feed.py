#!/usr/bin/env python3

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import io
import json
from pathlib import Path
import re
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
    self.assertTrue(message.startswith("Job Title: Accounting Intern"))

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

  def test_group_feed_message_recovers_title_from_description(self) -> None:
    message = jobscout.group_feed.format_group_feed_message(self.candidate(
      role="Unknown role",
      company="Unknown employer",
      description="RE-ADVERTISEMENT – HEAVY-DUTY MECHANIC Applicants who applied previously are kindly requested not to reapply.",
    ))

    self.assertTrue(message.startswith("Job Title: Heavy-Duty Mechanic"))
    self.assertNotIn("Job Title: Unknown role", message)

  def test_group_feed_message_collapses_repeated_skills_and_closing_date_sections(self) -> None:
    message = jobscout.group_feed.format_group_feed_message(self.candidate(
      role="Control Room Supervisor",
      location="Harare",
      company="Black Shark Protection Services",
      closing="31 Jul 2026",
      applicationEmails=["hrofficer@blackshark.co.zw"],
      applicationEmail="hrofficer@blackshark.co.zw",
      description=(
        "Job Title: Control Room Supervisor\n\n"
        "Location: Harare\n\n"
        "Company: Black Shark Protection Services\n\n"
        "Job Description The Control Room Supervisor supervises daily control room operations.\n\n"
        "Qualifications and Experience\n\n"
        "Educational\n\n"
        "Requirements\n\n"
        "Minimum of 5 O-Level passes. Professional Qualifications Certificate or Diploma in Security Management. "
        "Experience At least three (3) to five (5) years' experience. Competencies and\n\n"
        "Skills\n\n"
        "Technology\n\n"
        "Skills\n\n"
        "Proficient in operating and monitoring CCTV systems.\n\n"
        "Communication\n\n"
        "Skills\n\n"
        "Able to communicate effectively with security staff, employees, management, and the general public.\n\n"
        "How to Apply\n\n"
        "Submit CVs and certified copies by Email: hrofficer@blackshark.co.zw Or Hand Deliver To: "
        "Black Shark Head Office 26 Five Avenue Harare.\n\n"
        "Closing Date\n\n"
        "31 July 2026 Please note: Only shortlisted candidates will be contacted.\n\n"
        "Application\n\n"
        "Closing Date: 31 Jul 2026\n\n"
        "Join the group for more jobs: https://chat.whatsapp.com/JZvh9lwFOvc3N2sSs8F1QD"
      ),
    ), invite_url="https://chat.whatsapp.com/JZvh9lwFOvc3N2sSs8F1QD")

    self.assertEqual(sum(1 for line in message.splitlines() if line.strip() == "Skills"), 1)
    self.assertEqual(len(re.findall(r"(?im)^Closing Date\b", message)), 1)
    self.assertEqual(len(re.findall(r"(?im)^Job Title:", message)), 1)
    self.assertEqual(len(re.findall(r"(?im)^Location:", message)), 1)
    self.assertIn("\nCompany: Black Shark Protection Services\n", message)
    self.assertIn("\nTechnology\n", message)
    self.assertIn("\nCommunication\n", message)
    self.assertIn("hrofficer@blackshark.co.zw", message)
    self.assertIn("Hand Deliver To: Black Shark Head Office 26 Five Avenue Harare", message)
    self.assertEqual(message.count("Join the group for more jobs:"), 1)

  def test_group_feed_message_treats_abbreviated_and_full_closing_dates_as_same_date(self) -> None:
    message = jobscout.group_feed.format_group_feed_message(self.candidate(
      closing="31 Jul 2026",
      description="How to Apply\n\nEmail jobs@example.co.zw.\n\nClosing Date\n\n31 July 2026",
    ))

    self.assertEqual(len(re.findall(r"(?im)^Closing Date\b", message)), 1)
    self.assertNotIn("Closing Date: 31 Jul 2026", message)

  def test_group_feed_message_summarizes_long_description_without_losing_key_details(self) -> None:
    invite = "https://chat.whatsapp.com/JZvh9lwFOvc3N2sSs8F1QD"
    message = jobscout.group_feed.format_group_feed_message(self.candidate(
      source="everyday-vacancies-whatsapp",
      sourceUrl="https://whatsapp.com/channel/example/hiph",
      sourcePostUrl="https://whatsapp.com/channel/example/hiph",
      sourceChannelJid="123@newsletter",
      sourcePostId="hiph",
      role="Group Corporate Affairs Manager",
      company="Harare Institute of Public Health",
      location="Harare",
      closing="28 Jul 2026",
      applyLinks=[],
      contactLinks=[invite],
      applicationEmails=["admin@hiph.ac.zw"],
      applicationEmail="admin@hiph.ac.zw",
      description=(
        "Job Description Reports To: Group Chief Executive Officer "
        "About the Position Harare Institute of Public Health invites applications from suitably qualified, "
        "innovative and results-oriented professionals. The successful candidate will provide strategic leadership "
        "in corporate affairs by overseeing marketing, communications, public relations, human resources and "
        "administrative services across the Group. The role is responsible for strengthening the Group's corporate "
        "image, protecting its reputation, driving stakeholder engagement and ensuring effective management. "
        "Duties and Responsibilities Key Responsibilities The successful candidate will be responsible for: "
        "* Developing and implementing comprehensive corporate affairs, marketing and communication strategies. "
        "* Leading integrated marketing campaigns across digital, print, social media and public relations platforms. "
        "* Safeguarding and promoting the Group's corporate identity across all business units. "
        "* Serving as the primary liaison with the media and responding to media enquiries. "
        "* Developing internal communication strategies that promote employee engagement. "
        "* Leading recruitment, induction, employee relations, performance management and staff development. "
        "* Ensuring compliance with labour legislation and corporate policies. "
        "* Managing disciplinary processes, grievances and employee wellness initiatives. "
        "* Overseeing facilities, office assets, security services and general office operations. "
        "* Preparing departmental budgets and monitoring performance indicators. "
        "Qualifications and Experience Minimum Qualifications Applicants should possess: "
        "* A Diploma or Bachelor's Degree in Human Resources Management, Communications, Marketing or Business Studies. "
        "* A recognised professional qualification in Public Relations or Marketing is an added advantage. "
        "* Membership of a recognised professional body is an added advantage. "
        "Experience The ideal candidate should have: "
        "* A minimum of five to seven years' progressive experience in Human Resources, Marketing or Communications. "
        "* Demonstrated experience in corporate communications, brand management and stakeholder engagement. "
        "* Proven leadership experience in human resource management and organisational development. "
        "Application Closing Date: 28 Jul 2026 Email: admin@hiph.ac.zw "
        f"Join the group for more jobs: {invite}"
      ),
    ), invite_url=invite)

    self.assertLess(len(message), 2200)
    self.assertIn("\nOverview\n", message)
    self.assertIn("\nKey Responsibilities\n", message)
    self.assertIn("\nQualifications and Experience\n", message)
    self.assertIn("Developing and implementing comprehensive corporate affairs", message)
    self.assertIn("Diploma or Bachelor's Degree", message)
    self.assertIn("Closing Date: 28 Jul 2026", message)
    self.assertIn("Email: admin@hiph.ac.zw", message)
    self.assertNotIn("\n- Key\n", message)
    self.assertNotIn("Preparing departmental budgets", message)
    self.assertNotIn("Apply: https://chat.whatsapp.com", message)
    self.assertEqual(message.count("Join the group for more jobs:"), 1)

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
      source="vacancymail",
      sourceUrl=None,
      sourcePostUrl=None,
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
      applyLinks=["https://employer.example/jobs/receptionist/apply"],
      contactLinks=[],
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

  def test_group_feed_cache_skips_channel_post_with_only_group_invite(self) -> None:
    caregiver = self.candidate(
      candidateId="caregiver",
      source="everyday-vacancies-whatsapp",
      sourceUrl="https://whatsapp.com/channel/example/caregiver",
      sourcePostUrl="https://whatsapp.com/channel/example/caregiver",
      sourceChannelJid="123@newsletter",
      sourcePostId="caregiver",
      applyLinks=[],
      contactLinks=["https://chat.whatsapp.com/JZvh9lwFOvc3N2sSs8F1QD"],
      applicationEmails=[],
      applicationEmail=None,
      company="Unknown employer",
      role="Caregiver or Nurse Aid",
      location="Harare Westgate",
      closing=None,
      description=(
        "Looking for a stay-in caregiver to assist a five-year-old child with all daily activities. "
        "Salary $320 and ready to start."
      ),
    )
    kept, stats = jobscout.group_feed._dedupe_candidates([caregiver], 10)

    self.assertEqual(kept, [])
    self.assertEqual(stats["unactionableCount"], 1)
    self.assertEqual(stats["eligibleCount"], 0)

  def quality_candidate(self, **overrides) -> dict:
    """A fully structured advert that clears the strict quality bar."""
    data = self.candidate(
      candidateId="quality-1",
      role="Accountant",
      company="Tiru Fresh Finance",
      location="Harare",
      closing=FUTURE_CLOSING,
      sourceUrl="https://vacancymail.co.zw/jobs/accountant/",
      applyLinks=["https://vacancymail.co.zw/jobs/accountant/"],
      applicationEmails=["hr@tirufresh.co.zw"],
      applicationEmail="hr@tirufresh.co.zw",
      description=(
        "About Us We are hiring an Accountant to join our finance team. "
        "Key Responsibilities - Prepare monthly management accounts. "
        "- Reconcile ledgers and process payments. "
        "Qualifications and Experience - Degree in Accounting. "
        "- At least three years of relevant experience."
      ),
    )
    data.update(overrides)
    return data

  def test_candidate_meets_quality_standard(self) -> None:
    self.assertTrue(jobscout.group_feed.candidate_meets_quality_standard(self.quality_candidate()))

    # The baseline fixture has no Qualifications/Experience section.
    self.assertFalse(jobscout.group_feed.candidate_meets_quality_standard(self.candidate()))

    self.assertFalse(
      jobscout.group_feed.candidate_meets_quality_standard(self.quality_candidate(location=""))
    )
    # Unknown deadline (none supplied) and expired deadlines both fail.
    self.assertFalse(
      jobscout.group_feed.candidate_meets_quality_standard(self.quality_candidate(closing=None))
    )
    self.assertFalse(
      jobscout.group_feed.candidate_meets_quality_standard(self.quality_candidate(closing="1 January 2020"))
    )
    # No actionable contact: no email, no apply link, and an empty source page.
    self.assertFalse(
      jobscout.group_feed.candidate_meets_quality_standard(self.quality_candidate(
        applicationEmails=[], applicationEmail=None, applyLinks=[], sourceUrl="",
      ))
    )
    # Missing the Qualifications section.
    self.assertFalse(
      jobscout.group_feed.candidate_meets_quality_standard(self.quality_candidate(
        description="About Us We need an Accountant. Key Responsibilities - Prepare monthly accounts and reconcile ledgers daily.",
      ))
    )
    # Missing the Responsibilities section.
    self.assertFalse(
      jobscout.group_feed.candidate_meets_quality_standard(self.quality_candidate(
        description="About Us We need an Accountant. Qualifications and Experience - Degree in Accounting with three years of experience.",
      ))
    )

  def test_group_feed_cache_orders_quality_first_without_dropping(self) -> None:
    plain = self.candidate(
      candidateId="plain-1",
      role="Poultry Supervisor",
      company="Tiru Fresh",
      sourceUrl="https://vacancymail.co.zw/jobs/poultry-supervisor/",
      applyLinks=["https://vacancymail.co.zw/jobs/poultry-supervisor/"],
    )
    quality = self.quality_candidate()
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      with mock.patch(
        "jobscout.group_feed.fetch_whatsapp_channel_candidates_with_results",
        return_value=([], {"everyday-vacancies-whatsapp": {"status": "ok", "rawCount": 0, "durationMs": 1}}),
      ), mock.patch(
        "jobscout.group_feed.fetch_scraped_candidates_with_results",
        # Deliberately list the non-quality advert first; ordering must float quality up.
        return_value=([plain, quality], {"vacancymail": {"status": "ok", "rawCount": 2, "durationMs": 1}}),
      ):
        result = jobscout.group_feed.build_group_feed_cache("run-quality", root=root, limit=10)

      cache = json.loads((root / "latest.json").read_text(encoding="utf-8"))
      self.assertTrue(result["ok"])
      # Nothing is dropped; the quality advert is simply ordered first.
      self.assertEqual(cache["candidateCount"], 2)
      self.assertEqual([c["candidateId"] for c in cache["candidates"]], ["quality-1", "plain-1"])
      self.assertTrue(cache["candidates"][0]["groupFeedQuality"])
      self.assertFalse(cache["candidates"][1]["groupFeedQuality"])
      self.assertEqual(result["sourceResults"]["_filter"]["qualityCount"], 1)
      self.assertEqual(result["sourceResults"]["_filter"]["eligibleCount"], 2)

  def test_deliver_group_feed_relaxes_to_fill_cap_quality_first(self) -> None:
    quality = [
      self.quality_candidate(
        candidateId=f"quality-{index}",
        role=f"Accountant {index}",
        company=f"Quality Co {index}",
        sourceUrl=f"https://vacancymail.co.zw/jobs/quality-{index}/",
        applyLinks=[f"https://vacancymail.co.zw/jobs/quality-{index}/"],
      )
      for index in range(2)
    ]
    plain = [
      self.candidate(
        candidateId=f"plain-{index}",
        role=f"Poultry Supervisor {index}",
        company=f"Plain Co {index}",
        sourceUrl=f"https://vacancymail.co.zw/jobs/plain-{index}/",
        applyLinks=[f"https://vacancymail.co.zw/jobs/plain-{index}/"],
      )
      for index in range(4)
    ]
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      with mock.patch(
        "jobscout.group_feed.fetch_whatsapp_channel_candidates_with_results",
        return_value=([], {"everyday-vacancies-whatsapp": {"status": "ok", "rawCount": 0, "durationMs": 1}}),
      ), mock.patch(
        "jobscout.group_feed.fetch_scraped_candidates_with_results",
        # Non-quality adverts first, so only ordering (not source order) can pass this.
        return_value=(plain + quality, {"vacancymail": {"status": "ok", "rawCount": 6, "durationMs": 1}}),
      ):
        jobscout.group_feed.build_group_feed_cache("run-fill", root=root, limit=80)

      args = argparse.Namespace(
        target="120363000000000000@g.us",
        invite_url="https://chat.whatsapp.com/example",
        group_feed_root=root,
        cache_max_age_seconds=3600,
        max_messages=5,
        send_delay_seconds=0,
        dry_run=False,
      )
      with mock.patch.object(jobscout.commands_group_feed, "send_whatsapp_message", return_value={"messageId": "wa"}) as send:
        result = job_scout.cmd_deliver_group_feed(args)

      # Only two adverts clear the strict bar; the cap is still filled to five.
      self.assertEqual(result["sentCount"], 5)
      self.assertEqual(send.call_count, 5)
      first_two = "\n".join(call.args[1] for call in send.call_args_list[:2])
      self.assertIn("Job Title: Accountant 0", first_two)
      self.assertIn("Job Title: Accountant 1", first_two)

  def test_group_feed_message_cleans_bullet_fragments(self) -> None:
    # Reconstructs the scraper fragments from the two reference adverts: lone-emoji
    # bullets, a dangling truncated head ("Previous"), and a truncated head whose
    # continuation was split onto the next bullet ("Proven" / "in security ...").
    message = jobscout.group_feed.format_group_feed_message(self.candidate(
      role="Security Supervisor",
      location="Harare",
      company="A security company",
      closing=FUTURE_CLOSING,
      applicationEmails=["humanresourcesstrike@gmail.com"],
      applicationEmail="humanresourcesstrike@gmail.com",
      description=(
        "Overview A security company is looking for disciplined Security Supervisors.\n"
        "Key Responsibilities\n"
        "- 📌\n"
        "- Supervise and monitor security guards at various client sites\n"
        "- Conduct regular patrols and inspections\n"
        "Qualifications and Experience\n"
        "- 🎓\n"
        "- Diploma or equivalent in a related field\n"
        "- Proven\n"
        "- in security supervision or security operations\n"
        "- Previous\n"
      ),
    ))

    # Lone-emoji bullets are gone.
    for emoji in ("📌", "🎓"):
      self.assertNotIn(emoji, message)
    # The truncated continuation is merged back into one complete bullet.
    self.assertIn("- Proven in security supervision or security operations", message)
    # Dangling single-word truncated heads are dropped.
    self.assertNotIn("- Previous", message)
    self.assertNotIn("\n- Proven\n", message)
    self.assertNotIn("\n- in security", message)
    # Real bullets are preserved intact.
    self.assertIn("- Supervise and monitor security guards at various client sites", message)
    self.assertIn("- Diploma or equivalent in a related field", message)

  def test_group_feed_message_keeps_legitimate_single_word_skill_bullets(self) -> None:
    message = jobscout.group_feed.format_group_feed_message(self.candidate(
      role="Accounts Clerk",
      description=(
        "Overview We are hiring an Accounts Clerk.\n"
        "Key Responsibilities\n"
        "- Handle filing and maintain records.\n"
        "Skills\n"
        "- Communication\n"
        "- Accounting\n"
      ),
    ))

    self.assertIn("- Communication", message)
    self.assertIn("- Accounting", message)

  def test_group_feed_message_falls_back_to_source_page_when_only_apply_method(self) -> None:
    # VacancyMail/iHarare board jobs frequently have no email and only the listing
    # page as an apply link (== sourceUrl, which the formatter otherwise hides).
    # That page IS the apply mechanism, so it must be surfaced as Apply.
    message = jobscout.group_feed.format_group_feed_message(self.candidate(
      candidateId="human-capital-officer",
      source="vacancymail",
      sourceUrl="https://vacancymail.co.zw/jobs/human-capital-officer-69048/",
      applyLinks=["https://vacancymail.co.zw/jobs/human-capital-officer-69048/"],
      applicationEmails=[],
      applicationEmail=None,
      company="Rainbow Tourism Group",
      role="Human Capital Officer",
      location="Victoria Falls",
      closing=FUTURE_CLOSING,
      description=(
        "About Us Rainbow Tourism Group seeks a Human Capital Officer. "
        "Key Responsibilities Oversee end-to-end recruitment and onboarding."
      ),
    ))

    self.assertIn("Apply: https://vacancymail.co.zw/jobs/human-capital-officer-69048/", message)

  def test_group_feed_message_prefers_external_link_over_source_fallback(self) -> None:
    # When a real external apply link exists, the source page must NOT be added.
    message = jobscout.group_feed.format_group_feed_message(self.candidate(
      candidateId="client-service-consultant",
      source="iharare",
      sourceUrl="https://ihararejobs.com/job/client-service-consultant-6275/",
      applyLinks=[
        "https://ihararejobs.com/job/client-service-consultant-6275/",
        "https://oldmutual.wd3.myworkdayjobs.com/en-US/Careers/details/Funeral-Consultant_JR-83163",
      ],
      applicationEmails=[],
      applicationEmail=None,
      company="Old Mutual",
      role="Client Service Consultant",
      location="Harare",
      closing=FUTURE_CLOSING,
      description="About Us Old Mutual seeks a Client Service Consultant for its funeral services team.",
    ))

    self.assertIn("Apply: https://oldmutual.wd3.myworkdayjobs.com/en-US/Careers/details/Funeral-Consultant_JR-83163", message)
    self.assertNotIn("Apply: https://ihararejobs.com/job/client-service-consultant-6275/", message)

  def test_group_feed_message_omits_source_fallback_when_email_present(self) -> None:
    # An email is the apply method; do not add a redundant source page link.
    message = jobscout.group_feed.format_group_feed_message(self.candidate(
      candidateId="accounting-officer",
      source="vacancymail",
      sourceUrl="https://vacancymail.co.zw/jobs/accounting-officer-69045/",
      applyLinks=["https://vacancymail.co.zw/jobs/accounting-officer-69045/"],
      applicationEmails=["recruitment@cakefairy1.com"],
      applicationEmail="recruitment@cakefairy1.com",
      company="Cake Fairy",
      role="Accounting Officer",
      location="Bulawayo",
      closing=FUTURE_CLOSING,
      description="Job Description Oversee general accounting operations and verify financial transactions.",
    ))

    self.assertIn("Email: recruitment@cakefairy1.com", message)
    self.assertNotIn("Apply: https://vacancymail.co.zw/jobs/accounting-officer-69045/", message)

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
      self.assertEqual(state["schemaVersion"], 2)
      self.assertEqual(len(state["delivered"]["120363000000000000@g.us"]), 1)

      with mock.patch.object(jobscout.commands_group_feed, "send_whatsapp_message") as send_again:
        second = job_scout.cmd_deliver_group_feed(args)
      self.assertEqual(second["sentCount"], 0)
      self.assertEqual(second["skippedCount"], 1)
      send_again.assert_not_called()

  def test_delivered_state_is_tracked_per_target(self) -> None:
    # The same cached job must be deliverable to two groups independently: a send
    # to the first group must not suppress it for the second (Hustlers Marketplace).
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      candidate = self.candidate()
      jobscout.group_feed.write_group_feed_cache("cache-run", [candidate], {}, root)

      def args_for(target: str) -> argparse.Namespace:
        return argparse.Namespace(
          target=target,
          invite_url="https://chat.whatsapp.com/example",
          group_feed_root=root,
          cache_max_age_seconds=3600,
          max_messages=1,
          send_delay_seconds=0,
          dry_run=False,
        )

      primary = "120363000000000000@g.us"
      hustlers = "120363111111111111@g.us"
      with mock.patch.object(jobscout.commands_group_feed, "send_whatsapp_message", return_value={"messageId": "wa"}) as send:
        first = job_scout.cmd_deliver_group_feed(args_for(primary))
        second = job_scout.cmd_deliver_group_feed(args_for(hustlers))

      self.assertEqual(first["sentCount"], 1)
      self.assertEqual(second["sentCount"], 1)
      self.assertEqual(send.call_count, 2)
      state = json.loads((root / "delivered.json").read_text(encoding="utf-8"))
      key = jobscout.group_feed.candidate_delivery_key(candidate)
      self.assertIn(key, state["delivered"][primary])
      self.assertIn(key, state["delivered"][hustlers])

      # A repeat run for either target now skips (already delivered to that group).
      with mock.patch.object(jobscout.commands_group_feed, "send_whatsapp_message") as send_again:
        repeat = job_scout.cmd_deliver_group_feed(args_for(hustlers))
      self.assertEqual(repeat["sentCount"], 0)
      self.assertEqual(repeat["skippedCount"], 1)
      send_again.assert_not_called()

  def test_delivered_state_migrates_legacy_flat_schema(self) -> None:
    # A schema-1 file stored delivery as a flat {candidateKey: record} map. It must
    # migrate to the per-target shape, preserving which jobs each group already got.
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      candidate = self.candidate()
      key = jobscout.group_feed.candidate_delivery_key(candidate)
      target = "120363000000000000@g.us"
      legacy = {
        "schemaVersion": 1,
        "updatedAt": "2026-01-01T00:00:00+00:00",
        "delivered": {
          key: {"deliveredAt": "2026-01-01T00:00:00+00:00", "target": target, "role": "Poultry Supervisor"},
        },
      }
      (root / "delivered.json").write_text(json.dumps(legacy), encoding="utf-8")

      state = jobscout.group_feed.load_delivered_state(root)
      self.assertEqual(state["schemaVersion"], 2)
      self.assertIn(key, state["delivered"][target])

      # The migrated legacy entry means the primary group skips this job, while a
      # different group (Hustlers Marketplace) still receives it.
      jobscout.group_feed.write_group_feed_cache("cache-run", [candidate], {}, root)
      with mock.patch.object(jobscout.commands_group_feed, "send_whatsapp_message", return_value={"messageId": "wa"}) as send:
        primary = job_scout.cmd_deliver_group_feed(argparse.Namespace(
          target=target, invite_url="https://chat.whatsapp.com/example", group_feed_root=root,
          cache_max_age_seconds=3600, max_messages=1, send_delay_seconds=0, dry_run=False,
        ))
        hustlers = job_scout.cmd_deliver_group_feed(argparse.Namespace(
          target="120363111111111111@g.us", invite_url="https://chat.whatsapp.com/example", group_feed_root=root,
          cache_max_age_seconds=3600, max_messages=1, send_delay_seconds=0, dry_run=False,
        ))
      self.assertEqual(primary["sentCount"], 0)
      self.assertEqual(primary["skippedCount"], 1)
      self.assertEqual(hustlers["sentCount"], 1)
      send.assert_called_once()

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

  def test_deliver_group_feed_refreshes_a_stale_cache_before_sending(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      old = self.candidate(candidateId="old", sourceUrl="https://example.com/old")
      fresh = self.candidate(candidateId="fresh", sourceUrl="https://example.com/fresh")
      jobscout.group_feed.write_group_feed_cache("old-cache", [old], {}, root)
      cache_path = root / "latest.json"
      stale = json.loads(cache_path.read_text(encoding="utf-8"))
      stale["createdAt"] = "2020-01-01T00:00:00+00:00"
      cache_path.write_text(json.dumps(stale), encoding="utf-8")
      args = argparse.Namespace(
        target="120363000000000000@g.us",
        invite_url="https://chat.whatsapp.com/example",
        group_feed_root=root,
        cache_max_age_seconds=3600,
        refresh_if_stale=True,
        group_feed_limit=80,
        max_messages=1,
        send_delay_seconds=0,
        dry_run=False,
      )
      with mock.patch.object(jobscout.group_feed, "fetch_group_feed_candidates", return_value=([fresh], {})), \
           mock.patch.object(jobscout.commands_group_feed, "send_whatsapp_message", return_value={"messageId": "wa-fresh"}) as send:
        result = job_scout.cmd_deliver_group_feed(args)

      self.assertTrue(result["cacheRefreshed"])
      self.assertEqual(result["sentCount"], 1)
      self.assertIn("Poultry Supervisor", send.call_args.args[1])
      refreshed = json.loads(cache_path.read_text(encoding="utf-8"))
      self.assertEqual(refreshed["candidates"][0]["candidateId"], "fresh")

  def test_empty_refresh_preserves_existing_stale_cache(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      old = self.candidate(candidateId="old", sourceUrl="https://example.com/old")
      jobscout.group_feed.write_group_feed_cache("old-cache", [old], {}, root)
      cache_path = root / "latest.json"
      stale = json.loads(cache_path.read_text(encoding="utf-8"))
      stale["createdAt"] = "2020-01-01T00:00:00+00:00"
      cache_path.write_text(json.dumps(stale), encoding="utf-8")
      before = cache_path.read_bytes()
      args = argparse.Namespace(
        target="120363000000000000@g.us",
        invite_url="https://chat.whatsapp.com/example",
        group_feed_root=root,
        cache_max_age_seconds=3600,
        refresh_if_stale=True,
        group_feed_limit=80,
        max_messages=1,
        send_delay_seconds=0,
        dry_run=False,
      )
      with mock.patch.object(jobscout.group_feed, "fetch_group_feed_candidates", return_value=([], {})):
        with self.assertRaisesRegex(SystemExit, "produced no candidates"):
          job_scout.cmd_deliver_group_feed(args)

      self.assertEqual(cache_path.read_bytes(), before)

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
    self.assertFalse(args.refresh_if_stale)

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
