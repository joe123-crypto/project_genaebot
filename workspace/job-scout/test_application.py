#!/usr/bin/env python3

from __future__ import annotations

import unittest

from jobscout.application import build_user_summary, clean_role, cover_letter_text
from jobscout.formality import employer_formality
from jobscout.textutil import display_job_title

SUBSCRIBER = {"profile": {"displayName": "Joseph Munemo"}}
# The real messy role from a WhatsApp post: a "looking for ..." blurb truncated
# mid-word at 120 chars.
BLURB_ROLE = (
  "highly organised and tech-savvy Personal Assistant to support day-to-day "
  "operations and executive functions. The ideal c"
)


def letter(candidate: dict, preferences: dict | None = None) -> str:
  return cover_letter_text(SUBSCRIBER, preferences or {}, candidate)


class CleanRoleTests(unittest.TestCase):
  def test_clean_title_kept(self) -> None:
    self.assertEqual(clean_role("Personal Assistant"), "Personal Assistant")
    self.assertEqual(clean_role("IT Support Technician"), "IT Support Technician")
    self.assertEqual(clean_role("Assistant to the CEO"), "Assistant to the CEO")
    self.assertEqual(clean_role("Officer for Finance"), "Officer for Finance")

  def test_hiring_headline_extracts_role(self) -> None:
    self.assertEqual(
      clean_role("CLIMAtlantic is hiring a Climate Adaptation Support Assistant"),
      "Climate Adaptation Support Assistant",
    )
    self.assertEqual(
      clean_role("Cimas Health Group is hiring: Internal Auditor | Closing Date: 10 July 2026"),
      "Internal Auditor",
    )
    self.assertEqual(
      clean_role("Aviation Ground Services is hiring: Accounts Clerk | Apply by 13 July 2026"),
      "Accounts Clerk",
    )

  def test_recruiting_suffix_removed(self) -> None:
    self.assertEqual(clean_role("Human Resources Officer to join them soon."), "Human Resources Officer")
    self.assertEqual(clean_role("Accounting Intern to join our team"), "Accounting Intern")

  def test_unknown_role_sentinel(self) -> None:
    self.assertEqual(clean_role("Unknown role"), "advertised role")

  def test_empty_role(self) -> None:
    self.assertEqual(clean_role(""), "advertised role")
    self.assertEqual(clean_role(None), "advertised role")

  def test_blurb_falls_back(self) -> None:
    self.assertEqual(clean_role(BLURB_ROLE), "advertised role")

  def test_first_clause_when_short(self) -> None:
    self.assertEqual(clean_role("Driver. Must have license"), "Driver")


class DisplayJobTitleTests(unittest.TestCase):
  def title(self, description: str, role: str = "Unknown role", company: str = "Unknown employer") -> str | None:
    return display_job_title({"role": role, "company": company, "description": description})

  def test_recovers_titles_from_live_channel_headlines(self) -> None:
    self.assertEqual(
      self.title("RE-ADVERTISEMENT – HEAVY-DUTY MECHANIC Applicants who applied previously are kindly requested not to reapply."),
      "Heavy-Duty Mechanic",
    )
    self.assertEqual(
      self.title("RE-ADVERTISEMENT – WAREHOUSE ASSISTANT Applicants who applied previously are kindly requested not to reapply."),
      "Warehouse Assistant",
    )
    self.assertEqual(
      self.title("Team Leader (Underwriting) • Old Mutual Zimbabwe • Expires 26 Jul 2026 • Harare • Full Time"),
      "Team Leader (Underwriting)",
    )
    self.assertEqual(
      self.title("Attachment Students* - Human Resources - Public Relations - Economics - Procurement - Business Development"),
      "Attachment Students",
    )

  def test_recovers_titles_from_recruiting_phrases(self) -> None:
    self.assertEqual(
      self.title(
        "We’re hiring international truck drivers! Swift is inviting suitably qualified candidates to apply.",
        role="international truck drivers! 🚛",
      ),
      "International Truck Drivers",
    )
    self.assertEqual(
      self.title("Sister Charge* WE'RE HIRING! Mutapa Energy Resources is inviting applications."),
      "Sister Charge",
    )

  def test_multi_role_headlines_are_professional(self) -> None:
    self.assertEqual(
      self.title(
        "Gates Foundation Jobs 2026 | 20 International Career Opportunities Now Open",
        company="Gates Foundation",
      ),
      "Multiple roles at Gates Foundation",
    )
    self.assertEqual(
      self.title("WE'RE HIRING FULL TIME Bulawayo Group Inventory Controller x 1 Accounts Clerk x 1"),
      "Multiple roles",
    )

  def test_non_job_prose_has_no_display_title(self) -> None:
    self.assertIsNone(self.title(
      "Thank you to everyone who participated in my survey. I genuinely appreciate every single vote.",
      role="just a paycheck. We spend nearly a third of our lives at work.",
    ))


class CoverLetterTests(unittest.TestCase):
  def test_known_company_and_role(self) -> None:
    text = letter({"role": "Personal Assistant", "company": "Acme Ltd"})
    self.assertIn("Dear Hiring Team at Acme Ltd,", text)
    self.assertIn("I am applying for the Personal Assistant position at Acme Ltd.", text)

  def test_hiring_headline_uses_clean_role(self) -> None:
    text = letter({
      "role": "Cimas Health Group is hiring: Internal Auditor | Closing Date: 10 July 2026",
      "company": "Cimas Health Group",
    })
    self.assertIn("I am applying for the Internal Auditor position at Cimas Health Group.", text)
    self.assertNotIn("is hiring", text)
    self.assertNotIn("Closing Date", text)

  def test_cover_letter_uses_natural_dash_punctuation(self) -> None:
    text = letter({"role": "Control Room Operator – Dry Mortar Operations", "company": "Acme — Zimbabwe"})
    self.assertIn("Dear Hiring Team at Acme - Zimbabwe,", text)
    self.assertIn("I am applying for the Control Room Operator - Dry Mortar Operations position at Acme - Zimbabwe.", text)
    self.assertNotIn("—", text)
    self.assertNotIn("–", text)

  def test_recruiting_suffix_uses_clean_role(self) -> None:
    text = letter({"role": "Human Resources Officer to join them soon.", "company": "Unknown employer"})
    self.assertIn("I am applying for the Human Resources Officer position.", text)
    self.assertNotIn("to join them soon", text)

  def test_unknown_employer_never_referenced(self) -> None:
    text = letter({"role": "Personal Assistant", "company": "Unknown employer"})
    self.assertIn("Dear Hiring Team,", text)
    self.assertIn("I am applying for the Personal Assistant position.", text)
    self.assertNotIn("Unknown employer", text)
    self.assertNotIn(" at ", text.split("position")[0])

  def test_missing_company_treated_as_unknown(self) -> None:
    text = letter({"role": "Personal Assistant"})
    self.assertIn("Dear Hiring Team,", text)
    self.assertNotIn(" at ", text.split("position")[0])

  def test_blurb_role_produces_clean_letter(self) -> None:
    text = letter({"role": BLURB_ROLE, "company": "Unknown employer"})
    self.assertIn("I am applying for the Personal Assistant position.", text)
    self.assertNotIn("The ideal c", text)

  def test_no_doubled_the(self) -> None:
    for candidate in (
      {"role": "Unknown role", "company": "Unknown employer"},
      {"role": "", "company": "Acme Ltd"},
      {"role": BLURB_ROLE, "company": "Acme Ltd"},
    ):
      self.assertNotIn("the the", letter(candidate))

  def test_signed_with_display_name(self) -> None:
    text = letter({"role": "Personal Assistant", "company": "Acme Ltd"})
    self.assertTrue(text.rstrip().endswith("Joseph Munemo"))


class FormalityTests(unittest.TestCase):
  def test_government_employers_are_formal(self) -> None:
    for company in (
      "Zimbabwe Mining Development Corporation",
      "Ministry of Finance",
      "Public Service Commission",
      "Zimbabwe Revenue Authority",
      "Reserve Bank of Zimbabwe",
      "Bulawayo City Council",
    ):
      self.assertEqual(employer_formality({"company": company}), "formal", company)

  def test_ordinary_employers_are_standard(self) -> None:
    for company in ("Bright Cafe", "Acme Retail", "Acme Ltd", "Nimble Software"):
      self.assertEqual(employer_formality({"company": company}), "standard", company)

  def test_bare_corporation_is_not_formal(self) -> None:
    # A plain "Corporation" suffix is a common private-company shape.
    self.assertEqual(employer_formality({"company": "Delta Beverages Corporation"}), "standard")

  def test_signal_from_role_or_description(self) -> None:
    self.assertEqual(employer_formality({"company": "", "role": "Accountant, Ministry of Health"}), "formal")
    self.assertEqual(
      employer_formality({"company": "Acme", "description": "A role within the Public Service Commission."}),
      "formal",
    )

  def test_empty_candidate_is_standard(self) -> None:
    self.assertEqual(employer_formality({}), "standard")


class FormalCoverLetterTests(unittest.TestCase):
  def test_formal_greeting_and_closing(self) -> None:
    text = letter({"role": "Accounts Clerk", "company": "Ministry of Finance"})
    self.assertIn("Dear Sir/Madam,", text)
    self.assertIn("Yours faithfully,", text)
    self.assertNotIn("Dear Hiring Team", text)
    self.assertNotIn("Kind regards,", text)
    self.assertIn("Accounts Clerk position at Ministry of Finance", text)

  def test_formal_greeting_without_known_employer(self) -> None:
    text = letter({"role": "Accounts Clerk", "company": "Public Service Commission is recruiting"})
    self.assertIn("Dear Sir/Madam,", text)
    self.assertIn("Yours faithfully,", text)

  def test_standard_employer_keeps_existing_register(self) -> None:
    text = letter({"role": "Barista", "company": "Bright Cafe"})
    self.assertIn("Dear Hiring Team at Bright Cafe,", text)
    self.assertIn("Kind regards,", text)
    self.assertNotIn("Yours faithfully,", text)

  def test_formal_letter_passes_tailored_check(self) -> None:
    from jobscout.agent_dispatch import _letter_is_tailored
    import tempfile
    from pathlib import Path

    candidate = {"role": "Internal Auditor", "company": "Zimbabwe Revenue Authority"}
    text = letter(candidate)
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "cover-letter.txt"
      path.write_text(text, encoding="utf-8")
      self.assertTrue(_letter_is_tailored(path, candidate))


class UserSummaryTests(unittest.TestCase):
  def test_applied_role_cleaned(self) -> None:
    summary = build_user_summary(
      "2026-07-07",
      applied=[{"candidate": {"role": BLURB_ROLE, "company": "Acme Ltd"}}],
      action_required=[],
    )
    self.assertIn("Personal Assistant - APPLIED", summary)
    self.assertNotIn("The ideal c", summary)

  def test_action_required_role_cleaned(self) -> None:
    summary = build_user_summary(
      "2026-07-07",
      applied=[],
      action_required=[{"candidate": {"role": "Unknown role", "company": "Acme Ltd"}}],
    )
    self.assertIn("advertised role - ACTION NEEDED", summary)


if __name__ == "__main__":
  unittest.main()
