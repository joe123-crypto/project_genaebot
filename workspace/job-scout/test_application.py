#!/usr/bin/env python3

from __future__ import annotations

import unittest

from jobscout.application import build_user_summary, clean_role, cover_letter_text

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

  def test_unknown_role_sentinel(self) -> None:
    self.assertEqual(clean_role("Unknown role"), "advertised role")

  def test_empty_role(self) -> None:
    self.assertEqual(clean_role(""), "advertised role")
    self.assertEqual(clean_role(None), "advertised role")

  def test_blurb_falls_back(self) -> None:
    self.assertEqual(clean_role(BLURB_ROLE), "advertised role")

  def test_first_clause_when_short(self) -> None:
    self.assertEqual(clean_role("Driver. Must have license"), "Driver")


class CoverLetterTests(unittest.TestCase):
  def test_known_company_and_role(self) -> None:
    text = letter({"role": "Personal Assistant", "company": "Acme Ltd"})
    self.assertIn("Dear Hiring Team at Acme Ltd,", text)
    self.assertIn("I am applying for the Personal Assistant position at Acme Ltd.", text)

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
    self.assertIn("I am applying for the advertised role position.", text)
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


class UserSummaryTests(unittest.TestCase):
  def test_applied_role_cleaned(self) -> None:
    summary = build_user_summary(
      "2026-07-07",
      applied=[{"candidate": {"role": BLURB_ROLE, "company": "Acme Ltd"}}],
      action_required=[],
    )
    self.assertIn("advertised role - APPLIED", summary)
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
