#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).with_name("whatsapp-invite.py")
loader = importlib.machinery.SourceFileLoader("whatsapp_invite", str(MODULE_PATH))
spec = importlib.util.spec_from_loader("whatsapp_invite", loader)
assert spec and spec.loader
whatsapp_invite = importlib.util.module_from_spec(spec)
spec.loader.exec_module(whatsapp_invite)


def request_args(phone: str, message: str = "") -> argparse.Namespace:
    return argparse.Namespace(phone=phone, message=message)


class CmdRequestTests(unittest.TestCase):
    PHONE = "+263779528446"

    def test_first_contact_creates_pending_lead(self) -> None:
        store: dict = {"invites": {}, "members": {}}
        result, changed = whatsapp_invite.cmd_request(request_args(self.PHONE, "hello I want to apply"), store)
        self.assertTrue(changed)
        self.assertTrue(result["isNew"])
        self.assertEqual(result["status"], "pending")
        member = store["members"][self.PHONE]
        self.assertEqual(member["status"], "pending")
        self.assertEqual(member["source"], "lead")
        self.assertEqual(member["contactCount"], 1)
        self.assertEqual(member["lastMessage"], "hello I want to apply")

    def test_repeat_contact_dedupes_without_isnew(self) -> None:
        store: dict = {"invites": {}, "members": {}}
        whatsapp_invite.cmd_request(request_args(self.PHONE, "first"), store)
        result, changed = whatsapp_invite.cmd_request(request_args(self.PHONE, "second"), store)
        self.assertTrue(changed)
        self.assertFalse(result["isNew"])
        self.assertEqual(result["status"], "pending")
        member = store["members"][self.PHONE]
        self.assertEqual(member["contactCount"], 2)
        self.assertEqual(member["lastMessage"], "second")

    def test_request_never_downgrades_an_unlocked_member(self) -> None:
        store: dict = {"invites": {}, "members": {}}
        whatsapp_invite.cmd_grandfather(request_args(self.PHONE), store)
        result, _ = whatsapp_invite.cmd_request(request_args(self.PHONE, "please let me in"), store)
        self.assertFalse(result["isNew"])
        self.assertEqual(result["status"], "unlocked")
        self.assertEqual(store["members"][self.PHONE]["status"], "unlocked")

    def test_long_message_is_truncated(self) -> None:
        store: dict = {"invites": {}, "members": {}}
        whatsapp_invite.cmd_request(request_args(self.PHONE, "x" * 500), store)
        self.assertEqual(len(store["members"][self.PHONE]["lastMessage"]), 280)


class CmdListStatusFilterTests(unittest.TestCase):
    def test_list_filters_by_status(self) -> None:
        store: dict = {"invites": {}, "members": {}}
        whatsapp_invite.cmd_request(request_args("+263779528446"), store)
        whatsapp_invite.cmd_grandfather(request_args("+213563719936"), store)
        result, _ = whatsapp_invite.cmd_list(argparse.Namespace(status="pending"), store)
        phones = [m["phone"] for m in result["members"]]
        self.assertEqual(phones, ["+263779528446"])


if __name__ == "__main__":
    unittest.main()
