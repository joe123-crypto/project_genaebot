"""The group action bridge's contract (workspace/bin/console-group-action).

What it owns: the request it accepts, how a leave is handed to the monitor that
holds the socket, and what it says when no answer comes back.

That last one carries the most weight. A queued leave the monitor has not
answered yet is NOT a failure -- the request is on the queue and the account may
already be out of the group. Reporting it as failed would have an operator retry
something that had happened, so it is reported as unconfirmed and pinned here.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes them.
"""

import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = str(ROOT / "workspace/lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

JID = "263786627810-1599925488@g.us"


def load():
    """console-group-action has no .py extension, so it loads by path."""
    path = str(ROOT / "workspace/bin/console-group-action")
    loader = importlib.machinery.SourceFileLoader("console_group_action", path)
    spec = importlib.util.spec_from_loader("console_group_action", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class ParseRequestTest(unittest.TestCase):
    def setUp(self):
        self.module = load()

    def parse(self, payload):
        return self.module.parse_request(json.dumps(payload).encode("utf-8"))

    def test_a_leave_for_a_named_line(self):
        request = self.parse({"jid": JID, "account": "ads", "leave": True})
        self.assertEqual(request["jid"], JID)
        self.assertEqual(request["account"], "ads")
        self.assertIs(request["leave"], True)

    def test_the_default_line_is_assumed_when_none_is_named(self):
        self.assertEqual(self.parse({"jid": JID, "leave": True})["account"], "default")
        self.assertEqual(
            self.parse({"jid": JID, "account": None, "leave": True})["account"], "default")

    def test_a_request_that_asks_for_nothing_is_refused(self):
        # Answering "done" to this would tell an operator two boxes they meant
        # to tick had been honoured.
        with self.assertRaises(self.module.Refused):
            self.parse({"jid": JID})
        with self.assertRaises(self.module.Refused):
            self.parse({"jid": JID, "leave": False, "clearRecords": False})

    def test_only_a_group_jid_is_accepted(self):
        for value in ("", "263786627810@s.whatsapp.net", "../../etc@g.us",
                      "abc@g.us", "1-2-3@g.us", "x" * 130 + "@g.us"):
            with self.assertRaises(self.module.Refused, msg=value):
                self.parse({"jid": value, "leave": True})

    def test_only_a_line_id_is_accepted(self):
        for value in ("Ads", "-ads", "a ds", "__proto__", "a" * 65):
            with self.assertRaises(self.module.Refused, msg=value):
                self.parse({"jid": JID, "account": value, "leave": True})

    def test_unknown_keys_and_wrong_types_are_refused(self):
        with self.assertRaises(self.module.Refused):
            self.parse({"jid": JID, "leave": True, "collection": "groups"})
        with self.assertRaises(self.module.Refused):
            self.parse({"jid": JID, "leave": "yes"})
        with self.assertRaises(self.module.Refused):
            self.parse({"jid": JID, "leave": True, "actor": 7})

    def test_a_request_that_is_not_an_object_or_not_json_is_refused(self):
        with self.assertRaises(self.module.Refused):
            self.module.parse_request(b"[]")
        with self.assertRaises(self.module.Refused):
            self.module.parse_request(b"{nope")
        with self.assertRaises(self.module.Refused):
            self.module.parse_request(b"x" * (self.module.MAX_REQUEST_BYTES + 1))


class LeaveQueueTest(unittest.TestCase):
    def setUp(self):
        self.module = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"OPENCLAW_STATE_DIR": str(self.root)}
        self.queue = self.root / "runtime" / "whatsapp-group-actions" / "ads"

    def queued(self):
        return sorted((self.queue / "requests").glob("*.json"))

    def answer(self, body):
        """Stand in for the monitor: answer whatever request is on the queue."""
        [request] = self.queued()
        identifier = json.loads(request.read_text())["id"]
        (self.queue / "results" / (identifier + ".json")).write_text(
            json.dumps(dict(body, id=identifier)))
        request.unlink()

    def test_the_request_names_the_group_and_the_filename_never_does(self):
        state = {"answered": False}

        def sleep(_):
            if not state["answered"]:
                [request] = self.queued()
                payload = json.loads(request.read_text())
                self.assertEqual(payload["jid"], JID)
                # The jid carries an "@" and digits an operator chose; it must
                # never reach the filesystem as a path.
                self.assertNotIn("@", request.name)
                self.assertEqual(request.name, payload["id"] + ".json")
                self.answer({"ok": True, "already": False, "error": None})
                state["answered"] = True

        result = self.module.request_leave(JID, "ads", env=self.env, sleep=sleep)
        self.assertEqual(result, {"ok": True, "already": False, "error": None})

    def test_a_result_is_collected_and_removed(self):
        def sleep(_):
            if self.queued():
                self.answer({"ok": True, "already": True, "error": None})

        result = self.module.request_leave(JID, "ads", env=self.env, sleep=sleep)
        self.assertEqual(result, {"ok": True, "already": True, "error": None})
        self.assertEqual(list((self.queue / "results").glob("*.json")), [])

    def test_a_failure_keeps_the_monitor_s_own_word_for_it(self):
        def sleep(_):
            if self.queued():
                self.answer({"ok": False, "already": False, "error": "leave_failed"})

        self.assertEqual(self.module.request_leave(JID, "ads", env=self.env, sleep=sleep),
                         {"ok": False, "already": False, "error": "leave_failed"})

    def test_no_answer_in_time_is_unconfirmed_and_the_request_is_left_queued(self):
        ticks = iter([0.0, 0.0, 99.0])
        result = self.module.request_leave(
            JID, "ads", env=self.env, wait=1.0,
            clock=lambda: next(ticks), sleep=lambda _: None)

        self.assertEqual(result, {"ok": None, "already": False, "error": "unconfirmed"})
        # Left in place on purpose: the monitor may be reconnecting, and the
        # leave is still wanted when it comes back.
        self.assertEqual(len(self.queued()), 1)

    def test_each_line_has_its_own_queue(self):
        self.assertTrue(self.module.queue_dir("ads", self.env).endswith(
            os.path.join("runtime", "whatsapp-group-actions", "ads")))
        self.assertNotEqual(self.module.queue_dir("ads", self.env),
                            self.module.queue_dir("default", self.env))


class MainTest(unittest.TestCase):
    def setUp(self):
        self.module = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"OPENCLAW_STATE_DIR": str(self.root)}

    def run_main(self, payload):
        out = io.StringIO()
        code = self.module.main([], stdin=io.BytesIO(json.dumps(payload).encode()),
                                stdout=out, env=self.env)
        return code, json.loads(out.getvalue())

    def test_clearing_records_alone_never_touches_the_queue(self):
        state = self.root / "runtime" / "cron-state" / "cv-promo" / "state.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"sent": {JID: "2026-08-20"}, "resolved": {}}))

        code, payload = self.run_main({"jid": JID, "clearRecords": True})

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], 200)
        self.assertEqual(payload["cleared"]["campaigns"], {"cv-promo": ["sent"]})
        self.assertIs(payload["leave"]["requested"], False)
        self.assertFalse((self.root / "runtime" / "whatsapp-group-actions").exists())
        self.assertEqual(json.loads(state.read_text())["sent"], {})

    def test_a_refusal_names_a_status_the_api_can_pass_on(self):
        code, payload = self.run_main({"jid": "nope"})
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 400)
        self.assertEqual(payload["problems"], [])
        self.assertIn("group id", payload["error"])

    def test_no_firestore_credential_is_ever_read(self):
        # The whole point of this bridge being separate: it acts on the volume
        # and the gateway queue, and holds no key that could write config.
        source = (ROOT / "workspace/bin/console-group-action").read_text()
        for name in ("GCP_SERVICE_ACCOUNT_B64", "GCP_SERVICE_ACCOUNT_WRITE_B64",
                     "GCP_SERVICE_ACCOUNT_FILE"):
            self.assertNotIn(name, source)


if __name__ == "__main__":
    unittest.main()
