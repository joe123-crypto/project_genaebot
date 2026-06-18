#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path("/home/joseph/.openclaw/workspace/bin/genaie-mail")
loader = importlib.machinery.SourceFileLoader("genaie_mail", str(MODULE_PATH))
spec = importlib.util.spec_from_loader("genaie_mail", loader)
assert spec and spec.loader
genaie_mail = importlib.util.module_from_spec(spec)
spec.loader.exec_module(genaie_mail)


def args(**overrides: object) -> argparse.Namespace:
  base = {
    "to": ["hr@example.dz"],
    "subject": "Application",
    "from_email": None,
    "sender_uid": None,
    "sender_key": None,
    "use_owner_auth": False,
    "cc": None,
    "bcc": None,
    "reply_to": None,
    "body": "Hello",
    "body_file": None,
    "html": None,
    "html_file": None,
    "attach": None,
  }
  base.update(overrides)
  return argparse.Namespace(**base)


class GenaieMailPayloadTests(unittest.TestCase):
  def test_from_email_still_maps_to_from_email(self) -> None:
    payload = genaie_mail.build_payload(args(from_email="applicant@example.com"))

    self.assertEqual(payload["fromEmail"], "applicant@example.com")
    self.assertNotIn("senderUid", payload)

  def test_sender_uid_maps_to_sender_uid(self) -> None:
    payload = genaie_mail.build_payload(args(sender_uid="firebase-uid-1"))

    self.assertEqual(payload["senderUid"], "firebase-uid-1")

  def test_sender_key_maps_to_sender_key(self) -> None:
    payload = genaie_mail.build_payload(args(sender_key="firebase:abc"))

    self.assertEqual(payload["senderKey"], "firebase:abc")

  def test_use_owner_auth_maps_to_boolean(self) -> None:
    payload = genaie_mail.build_payload(args(use_owner_auth=True))

    self.assertIs(payload["useOwnerAuth"], True)

  def test_dry_run_summary_does_not_print_sender_key(self) -> None:
    payload = genaie_mail.build_payload(args(sender_key="secret-sender-key"))
    summary = genaie_mail.dry_run_summary(payload, "https://app.test/internal/gmail/send")

    self.assertTrue(summary["hasSenderKey"])
    self.assertNotIn("secret-sender-key", str(summary))


if __name__ == "__main__":
  unittest.main()
