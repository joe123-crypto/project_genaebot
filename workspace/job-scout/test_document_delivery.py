#!/usr/bin/env python3

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from jobscout import api
from jobscout.application import (
  prepare_application_documents,
  store_application_artifacts,
  web_apply_payload,
)
from jobscout.delivery import cover_letter_html, pdf_page_count, render_html_to_pdf, send_application_email
import jobscout.subscriber


SUBSCRIBER = {
  "userId": "user-1",
  "whatsappPhone": "+263700000001",
  "whatsappPhoneHash": "phonehash1",
  "senderEmail": "applicant@example.com",
  "cvFileRef": "user-1/cv/base/cv.html",
  "cvHtmlRef": "user-1/cv/base/cv.html",
  "cvConversionStatus": "ready",
  "profile": {"displayName": "Test Applicant"},
}
CANDIDATE = {
  "candidateId": "candidate-1",
  "company": "Example Ltd",
  "role": "Driver",
  "sourceUrl": "https://jobs.example/apply",
  "applyLinks": ["https://jobs.example/apply"],
  "applicationEmail": "jobs@example.com",
}


class FakeResponse:
  def __init__(self, body: bytes, content_type: str = "application/pdf") -> None:
    self.body = body
    self.headers = {"Content-Type": content_type}

  def __enter__(self):
    return self

  def __exit__(self, *_args):
    return False

  def read(self) -> bytes:
    return self.body


class RenderClientTests(unittest.TestCase):
  def test_posts_raw_html_and_writes_exact_pdf(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      output = Path(tmp) / "cv.pdf"
      with mock.patch("jobscout.delivery.env_default") as env, mock.patch(
        "jobscout.delivery.urllib.request.urlopen",
        return_value=FakeResponse(b"%PDF-1.7 exact bytes"),
      ) as urlopen:
        env.side_effect = lambda name, default="": {
          "JOB_SCOUT_RENDER_PDF_URL": "https://worker.example/render-pdf",
          "JOB_SCOUT_BROWSER_TOKEN": "secret",
        }.get(name, default)
        result = render_html_to_pdf("<!doctype html><p>Tailored CV</p>", output)

      self.assertEqual(result, output)
      self.assertEqual(output.read_bytes(), b"%PDF-1.7 exact bytes")
      request = urlopen.call_args.args[0]
      self.assertEqual(request.full_url, "https://worker.example/render-pdf")
      self.assertEqual(request.data, b"<!doctype html><p>Tailored CV</p>")
      self.assertEqual(request.get_header("Content-type"), "text/html; charset=utf-8")
      self.assertEqual(request.get_header("Authorization"), "Bearer secret")

  def test_rejects_invalid_pdf_response(self) -> None:
    with tempfile.TemporaryDirectory() as tmp, mock.patch("jobscout.delivery.env_default") as env, mock.patch(
      "jobscout.delivery.urllib.request.urlopen",
      return_value=FakeResponse(b"not pdf", "application/json"),
    ):
      env.side_effect = lambda name, default="": {
        "JOB_SCOUT_RENDER_PDF_URL": "https://worker.example/render-pdf",
        "JOB_SCOUT_BROWSER_TOKEN": "secret",
      }.get(name, default)
      with self.assertRaisesRegex(RuntimeError, "invalid PDF"):
        render_html_to_pdf("<p>CV</p>", Path(tmp) / "cv.pdf")

  def test_pdf_page_count_is_graceful_when_unreadable(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      bad = Path(tmp) / "x.pdf"
      bad.write_bytes(b"not a real pdf")
      # An unreadable/absent PDF must not force a one-page rejection.
      self.assertEqual(pdf_page_count(bad), 1)

  def test_html_source_rejects_a_missing_file_path(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      existing = Path(tmp) / "cv.html"
      existing.write_text("<p>CV</p>", encoding="utf-8")

      self.assertEqual(jobscout.delivery._html_source("<p>raw</p>"), "<p>raw</p>")
      self.assertEqual(jobscout.delivery._html_source(str(existing)), "<p>CV</p>")
      with self.assertRaisesRegex(RuntimeError, "does not look like HTML"):
        jobscout.delivery._html_source(str(Path(tmp) / "deleted.html"))

  def test_validator_cap_matches_render_worker_cap(self) -> None:
    from jobscout import agent_dispatch

    self.assertEqual(agent_dispatch.MAX_HTML_BYTES, jobscout.delivery.MAX_RENDER_HTML_BYTES)


class CanonicalCvTests(unittest.TestCase):
  def setUp(self) -> None:
    jobscout.subscriber._cv_download_cache.clear()

  def test_ready_html_is_downloaded_and_cached_by_ref(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      dest = Path(tmp) / "cv.html"

      def download(_user_id, target):
        self.assertEqual(target, dest)
        target.write_text("<!doctype html><p>CV</p>", encoding="utf-8")
        return target

      with mock.patch("jobscout.subscriber.cv_local_dest", return_value=dest), mock.patch(
        "jobscout.subscriber.api.download_cv",
        side_effect=download,
      ) as fetch:
        first = jobscout.subscriber.subscriber_cv_html(SUBSCRIBER)
        second = jobscout.subscriber.subscriber_cv_html(SUBSCRIBER)

      self.assertEqual(first, str(dest))
      self.assertEqual(second, str(dest))
      fetch.assert_called_once()

  def test_pending_pdf_is_not_dispatchable_as_html(self) -> None:
    pending = {
      **SUBSCRIBER,
      "cvFileRef": "user-1/cv/pending/original.pdf",
      "cvHtmlRef": None,
      "cvConversionStatus": "pending",
    }
    with mock.patch("jobscout.subscriber.api.download_cv") as fetch:
      with self.assertRaisesRegex(RuntimeError, "not ready"):
        jobscout.subscriber.subscriber_cv_html(pending)
    fetch.assert_not_called()


class ApplicationDocumentTests(unittest.TestCase):
  def test_prepares_tailored_cv_and_optional_letter_pdf(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      tailored = root / "tailored.html"
      tailored.write_text("<!doctype html><p>Tailored CV</p>", encoding="utf-8")
      letter = root / "letter.txt"
      letter.write_text("Dear Hiring Team,\nTailored letter.\n", encoding="utf-8")

      def render(_source, output):
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"%PDF-1.7 rendered")
        return output

      with mock.patch("jobscout.application.application_artifact_dir", return_value=root / "attempt"), mock.patch(
        "jobscout.application.render_one_page_cv",
        side_effect=render,
      ) as cv_renderer, mock.patch(
        "jobscout.application.render_html_to_pdf",
        side_effect=render,
      ) as letter_renderer:
        documents = prepare_application_documents(
          SUBSCRIBER,
          CANDIDATE,
          letter,
          personalized_cv_html=tailored,
          include_cover_letter_pdf=True,
          attempt=2,
        )

      self.assertEqual(documents["coverLetterText"], letter)
      self.assertEqual(documents["cvPdf"].read_bytes(), b"%PDF-1.7 rendered")
      self.assertEqual(documents["coverLetterPdf"].read_bytes(), b"%PDF-1.7 rendered")
      # The CV goes through the single-page render helper with the tailored source.
      self.assertEqual(Path(cv_renderer.call_args.args[0]), tailored)
      letter_html = letter_renderer.call_args.args[0]
      self.assertIn("Tailored letter.", letter_html)

  def test_render_one_page_cv_styles_and_scales_until_it_fits(self) -> None:
    from jobscout import delivery
    with tempfile.TemporaryDirectory() as tmp:
      out = Path(tmp) / "cv.pdf"
      rendered: list[str] = []

      def render(source, output):
        rendered.append(source)
        Path(output).write_bytes(b"%PDF-1.7")
        return Path(output)

      # Two pages, still two, then one: it scales down until it fits.
      with mock.patch("jobscout.delivery.render_html_to_pdf", side_effect=render), mock.patch(
        "jobscout.delivery.pdf_page_count",
        side_effect=[2, 2, 1],
      ):
        delivery.render_one_page_cv("<html><head></head><body>Fits me@example.com</body></html>", out)

      self.assertEqual(len(rendered), 3)
      # The unstyled source is styled on the first render, then zoomed down.
      self.assertIn("<style>", rendered[0])
      self.assertIn(".skill", rendered[0])
      self.assertNotIn("zoom", rendered[0])
      self.assertIn("zoom: 0.92", rendered[1])
      self.assertIn("zoom: 0.85", rendered[2])

  def test_web_payload_separates_text_and_file_cover_letters(self) -> None:
    payload = web_apply_payload(
      SUBSCRIBER,
      {"locations": ["Harare, Zimbabwe"]},
      CANDIDATE,
      Path("/tmp/cover-letter.txt"),
      cv_pdf=Path("/tmp/tailored-cv.pdf"),
      cover_letter_pdf=Path("/tmp/cover-letter.pdf"),
    )
    self.assertEqual(payload["cvPath"], "/tmp/tailored-cv.pdf")
    self.assertEqual(payload["coverLetterTextPath"], "/tmp/cover-letter.txt")
    self.assertEqual(payload["coverLetterFilePath"], "/tmp/cover-letter.pdf")
    self.assertNotIn("coverLetterPath", payload)

  def test_email_attaches_exact_tailored_pdf(self) -> None:
    with mock.patch("jobscout.delivery.run_json_command", return_value={"messageId": "m1"}) as run, mock.patch(
      "jobscout.delivery.subscriber_cv_file",
    ) as fallback:
      send_application_email(
        SUBSCRIBER,
        CANDIDATE,
        Path("/tmp/cover-letter.txt"),
        Path("/tmp/tailored-cv.pdf"),
        Path("/tmp/cover-letter.pdf"),
      )

    command = run.call_args.args[0]
    attachments = [command[index + 1] for index, value in enumerate(command) if value == "--attach"]
    self.assertEqual(attachments, ["/tmp/tailored-cv.pdf", "/tmp/cover-letter.pdf"])
    fallback.assert_not_called()

  def test_email_fallback_renders_cv_and_cover_letter_before_attaching(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      letter = root / "cover-letter.txt"
      letter.write_text("Dear Hiring Team,\n\nA specific letter.\n\nKind regards,\nTest Applicant\n", encoding="utf-8")
      cv_html = root / "cv.html"
      cv_html.write_text("<!doctype html><head></head><body><p>Base CV</p></body>", encoding="utf-8")

      def render_file(_source, output):
        return Path(output)

      with mock.patch("jobscout.delivery.run_json_command", return_value={"messageId": "m1"}) as run, mock.patch(
        "jobscout.delivery.subscriber_cv_file",
        return_value=str(cv_html),
      ), mock.patch(
        "jobscout.delivery.render_html_to_pdf",
        side_effect=render_file,
      ) as render:
        send_application_email(SUBSCRIBER, CANDIDATE, letter)

      self.assertEqual(render.call_count, 2)
      # The base HTML is styled on the fly, then rendered to the applicant-named CV.
      styled_cv = render.call_args_list[0].args[0]
      self.assertIn("Base CV", styled_cv)
      self.assertIn("<style>", styled_cv)
      self.assertEqual(render.call_args_list[0].args[1], root / "Test-Applicant-CV.pdf")
      letter_html = render.call_args_list[1].args[0]
      self.assertIn("Application Letter", letter_html)
      command = run.call_args.args[0]
      attachments = [command[index + 1] for index, value in enumerate(command) if value == "--attach"]
      self.assertEqual(attachments, [str(root / "Test-Applicant-CV.pdf"), str(root / "Test-Applicant-Cover-Letter.pdf")])

  def test_email_rebuilds_legacy_pdf_cv_instead_of_attaching_verbatim(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      letter = root / "cover-letter.txt"
      letter.write_text("Dear Hiring Team,\n\nA specific letter.\n\nKind regards,\nTest Applicant\n", encoding="utf-8")
      # A stale browser print-to-PDF stored as the canonical CV.
      legacy_pdf = root / "cv.pdf"
      legacy_pdf.write_bytes(b"%PDF-1.7 stale")

      def convert(_pdf_path, html_path):
        # Extraction strips the chrome; return clean semantic HTML.
        Path(html_path).write_text(
          '<!doctype html><head></head><body><main data-cv-template="professional-v2"><p>Clean CV</p></main></body>',
          encoding="utf-8",
        )
        return Path(html_path)

      def render_file(_source, output):
        return Path(output)

      with mock.patch("jobscout.delivery.run_json_command", return_value={"messageId": "m1"}) as run, mock.patch(
        "jobscout.delivery.subscriber_cv_file",
        return_value=str(legacy_pdf),
      ), mock.patch(
        "jobscout.delivery.convert_pdf_to_cv_html",
        side_effect=convert,
      ) as convert_mock, mock.patch(
        "jobscout.delivery.render_html_to_pdf",
        side_effect=render_file,
      ) as render:
        send_application_email(SUBSCRIBER, CANDIDATE, letter)

      # The legacy PDF is re-extracted and re-rendered, never attached verbatim.
      convert_mock.assert_called_once()
      self.assertEqual(Path(convert_mock.call_args.args[0]), legacy_pdf)
      styled_cv = render.call_args_list[0].args[0]
      self.assertIn("<style>", styled_cv)
      self.assertNotIn("file://", styled_cv)
      command = run.call_args.args[0]
      attachments = [command[index + 1] for index, value in enumerate(command) if value == "--attach"]
      self.assertEqual(attachments[0], str(root / "Test-Applicant-CV.pdf"))

  def test_cover_letter_pdf_html_is_structured_and_has_no_browser_leaks(self) -> None:
    document = cover_letter_html("Dear Hiring Team,\n\nA specific letter.\n\nKind regards,\nTest Applicant\n")
    self.assertIn("<h1>Test Applicant</h1>", document)
    self.assertIn("Application Letter", document)
    self.assertNotIn("file://", document)
    self.assertNotIn("3:47 PM", document)

  def test_stores_exact_artifacts_after_record_and_only_optional_pdf(self) -> None:
    uploads = [
      {"artifactRef": "user-1/applications/app-1/2/cv.pdf"},
      {"artifactRef": "user-1/applications/app-1/2/cover-letter.txt"},
      {"artifactRef": "user-1/applications/app-1/2/cover-letter.pdf"},
    ]
    with mock.patch("jobscout.application.api.upload_application_artifact", side_effect=uploads) as upload:
      refs = store_application_artifacts(
        SUBSCRIBER,
        {"ok": True, "id": "app-1"},
        2,
        "/tmp/cv.pdf",
        "/tmp/cover-letter.txt",
        "/tmp/cover-letter.pdf",
      )

    self.assertEqual(set(refs), {"cv", "coverLetterText", "coverLetterPdf"})
    self.assertEqual(
      [call.args[3] for call in upload.call_args_list],
      ["cv", "cover_letter_text", "cover_letter_pdf"],
    )
    self.assertTrue(all(call.args[:3] == ("user-1", "app-1", 2) for call in upload.call_args_list))


class ArtifactApiTests(unittest.TestCase):
  def test_upload_contract_uses_server_owned_key_fields(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      cv = Path(tmp) / "cv.pdf"
      cv.write_bytes(b"%PDF-1.7 exact")
      with mock.patch("jobscout.api.request_multipart", return_value={"ok": True, "key": "stored"}) as request:
        result = api.upload_application_artifact("user-1", "app-1", 3, "cv", cv)

    self.assertTrue(result["ok"])
    url, fields, field_name, path, filename, content_type = request.call_args.args
    self.assertTrue(url.endswith("/internal/job-scout/application-artifact"))
    self.assertEqual(fields, {
      "userId": "user-1",
      "applicationId": "app-1",
      "attempt": "3",
      "kind": "cv",
    })
    self.assertEqual(field_name, "file")
    self.assertEqual(path, cv.resolve())
    self.assertEqual(filename, "cv.pdf")
    self.assertEqual(content_type, "application/pdf")

  def test_upload_converts_cli_exit_into_nonfatal_runtime_error(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      cv = Path(tmp) / "cv.pdf"
      cv.write_bytes(b"%PDF-1.7 exact")
      with mock.patch("jobscout.api.request_multipart", side_effect=SystemExit(1)):
        with self.assertRaisesRegex(RuntimeError, "could not store"):
          api.upload_application_artifact("user-1", "app-1", 1, "cv", cv)


class DocumentFilenameTests(unittest.TestCase):
  def test_names_pdfs_after_the_applicant(self) -> None:
    subscriber = {"profile": {"displayName": "Joseph Munemo"}}
    self.assertEqual(
      jobscout.subscriber.document_filename(subscriber, "cv"),
      "Joseph-Munemo-CV.pdf",
    )
    self.assertEqual(
      jobscout.subscriber.document_filename(subscriber, "cover_letter"),
      "Joseph-Munemo-Cover-Letter.pdf",
    )

  def test_falls_back_to_applicant_for_a_nameless_subscriber(self) -> None:
    self.assertEqual(
      jobscout.subscriber.document_filename({}, "cv"),
      "Applicant-CV.pdf",
    )


if __name__ == "__main__":
  unittest.main()
