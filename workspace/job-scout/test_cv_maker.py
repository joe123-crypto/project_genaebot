#!/usr/bin/env python3

from __future__ import annotations

import argparse
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from jobscout.commands_cv import CV_READY_MESSAGE, _cv_filename, _cv_identity, cmd_make_cv


DETAILS_TEXT = """\
Joseph Munemo
Software Engineer

CONTACT
+263 77 000 0000
joseph@example.com
Bindura, Zimbabwe

SKILLS
Python
JavaScript

EXPERIENCE
Software Engineer, Acme Ltd (2021 - present)
- Built the payments API

EDUCATION
BSc Computer Science, University of Zimbabwe (2020)
"""


def _fake_render(html_or_path, output):
  path = Path(output)
  path.write_bytes(b"%PDF-1.7 fake")
  return path


def _make_args(**overrides) -> argparse.Namespace:
  args = argparse.Namespace(phone="+263700000001", cv=None, details_file=None)
  for key, value in overrides.items():
    setattr(args, key, value)
  return args


class CvIdentityTests(unittest.TestCase):
  def test_identity_read_from_semantic_html(self) -> None:
    html = '<h1 data-cv-field="identity">Joseph Munemo</h1>'
    self.assertEqual(_cv_identity(html), "Joseph Munemo")

  def test_identity_unescapes_entities(self) -> None:
    html = '<h1 data-cv-field="identity">Ada &amp; Grace</h1>'
    self.assertEqual(_cv_identity(html), "Ada & Grace")

  def test_filename_from_name(self) -> None:
    html = '<h1 data-cv-field="identity">Joseph Munemo</h1>'
    self.assertEqual(_cv_filename(html), "Joseph-Munemo-CV.pdf")

  def test_filename_falls_back_when_no_identity(self) -> None:
    self.assertEqual(_cv_filename("<h1>no data attr</h1>"), "Applicant-CV.pdf")


class MakeCvArgumentTests(unittest.TestCase):
  def test_requires_a_phone(self) -> None:
    with self.assertRaisesRegex(SystemExit, "--phone is required"):
      cmd_make_cv(_make_args(phone="", details_file="/tmp/x.txt"))

  def test_requires_exactly_one_source(self) -> None:
    with self.assertRaisesRegex(SystemExit, "exactly one"):
      cmd_make_cv(_make_args())
    with self.assertRaisesRegex(SystemExit, "exactly one"):
      cmd_make_cv(_make_args(cv="/tmp/a.pdf", details_file="/tmp/b.txt"))

  def test_missing_details_file(self) -> None:
    with self.assertRaisesRegex(SystemExit, "--details-file does not exist"):
      cmd_make_cv(_make_args(details_file="/tmp/does-not-exist-cv-maker.txt"))

  def test_missing_cv_file(self) -> None:
    with self.assertRaisesRegex(SystemExit, "--cv file does not exist"):
      cmd_make_cv(_make_args(cv="/tmp/does-not-exist-cv-maker.pdf"))


class MakeCvFromDetailsTests(unittest.TestCase):
  def test_builds_html_renders_and_sends(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      details = Path(tmp) / "details.txt"
      details.write_text(DETAILS_TEXT, encoding="utf-8")
      with mock.patch("jobscout.delivery.render_one_page_cv", side_effect=_fake_render) as render, \
        mock.patch("jobscout.delivery.pdf_page_count", return_value=1), \
        mock.patch(
          "jobscout.delivery.send_whatsapp_document",
          return_value={"messageId": "wamid.1"},
        ) as send:
        result = cmd_make_cv(_make_args(details_file=str(details)))

      # The details text was turned into canonical CV HTML (same builder the PDF
      # path uses) before rendering.
      rendered_html = render.call_args.args[0]
      self.assertIn("Joseph Munemo", rendered_html)
      self.assertIn("Python", rendered_html)
      self.assertIn('data-cv-field="identity"', rendered_html)

      # Delivery went to the requester with the finished PDF and caption.
      send_args = send.call_args.args
      self.assertEqual(send_args[0], "+263700000001")
      self.assertEqual(Path(send_args[1]).name, "Joseph-Munemo-CV.pdf")
      self.assertEqual(send_args[2], CV_READY_MESSAGE)

      self.assertEqual(
        result,
        {
          "ok": True,
          "mode": "details",
          "delivered": True,
          "pages": 1,
          "filename": "Joseph-Munemo-CV.pdf",
          "messageId": "wamid.1",
        },
      )

  def test_reports_not_delivered_without_message_id(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      details = Path(tmp) / "details.txt"
      details.write_text(DETAILS_TEXT, encoding="utf-8")
      with mock.patch("jobscout.delivery.render_one_page_cv", side_effect=_fake_render), \
        mock.patch("jobscout.delivery.pdf_page_count", return_value=1), \
        mock.patch("jobscout.delivery.send_whatsapp_document", return_value={}):
        result = cmd_make_cv(_make_args(details_file=str(details)))
      self.assertTrue(result["ok"])
      self.assertFalse(result["delivered"])


class MakeCvRevampTests(unittest.TestCase):
  def test_converts_pdf_renders_and_sends(self) -> None:
    canonical = '<h1 data-cv-field="identity">Joseph Munemo</h1>'

    def fake_convert(pdf_path, html_path):
      Path(html_path).write_text(canonical, encoding="utf-8")
      return Path(html_path)

    with tempfile.TemporaryDirectory() as tmp:
      pdf = Path(tmp) / "cv.pdf"
      pdf.write_bytes(b"%PDF-1.7 source")
      with mock.patch("jobscout.commands_cv.convert_pdf_to_cv_html", side_effect=fake_convert) as convert, \
        mock.patch("jobscout.delivery.render_one_page_cv", side_effect=_fake_render) as render, \
        mock.patch("jobscout.delivery.pdf_page_count", return_value=1), \
        mock.patch(
          "jobscout.delivery.send_whatsapp_document",
          return_value={"messageId": "wamid.2"},
        ) as send:
        result = cmd_make_cv(_make_args(cv=str(pdf)))

      self.assertEqual(Path(convert.call_args.args[0]), pdf)
      self.assertEqual(render.call_args.args[0], canonical)
      self.assertEqual(send.call_args.args[0], "+263700000001")
      self.assertEqual(result["mode"], "revamp")
      self.assertTrue(result["delivered"])
      self.assertEqual(result["filename"], "Joseph-Munemo-CV.pdf")


if __name__ == "__main__":
  unittest.main()
