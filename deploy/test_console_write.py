"""The write bridge's own contract (workspace/bin/console-write).

What it owns: the request it accepts, the environment the write runs under, and
the payload it hands back -- a ``status`` the API can pass on, and an exit code
that never contradicts it. What a legal CHANGE is belongs to
workspace/lib/operator_writer.py and is tested there.

The write key matters most here. It is its own variable so that a campaign run,
which carries the read-only runtime key, cannot be made to write; this pins that
the bridge uses the write key and refuses rather than silently falling back to
whatever key happens to be in the environment.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes them.
"""

import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = str(ROOT / "workspace/lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import operator_config  # noqa: E402
import operator_writer  # noqa: E402


def load():
    """console-write has no .py extension, so it loads by path."""
    path = str(ROOT / "workspace/bin/console-write")
    loader = importlib.machinery.SourceFileLoader("console_write", path)
    spec = importlib.util.spec_from_loader("console_write", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


CW = load()
KEY = "eyJjbGllbnRfZW1haWwiOiAid3JpdGVyIn0="
REQUEST = {"collection": "groups", "id": "x@g.us", "fields": {"status": "on-hold"}}


class RequestParsing(unittest.TestCase):
    def refuse(self, raw):
        with self.assertRaises(operator_writer.WriteRefused) as caught:
            CW.parse_request(raw if isinstance(raw, bytes) else json.dumps(raw).encode())
        return str(caught.exception)

    def test_a_complete_request_is_accepted(self):
        parsed = CW.parse_request(json.dumps(dict(REQUEST, actor="joe")).encode())
        self.assertEqual(parsed["collection"], "groups")
        self.assertEqual(parsed["actor"], "joe")

    def test_what_a_request_must_carry(self):
        for missing in ("collection", "id", "fields"):
            request = dict(REQUEST)
            del request[missing]
            with self.subTest(missing=missing):
                self.assertIn(missing, self.refuse(request))

    def test_a_request_is_refused_rather_than_guessed_at(self):
        self.assertIn("JSON", self.refuse(b"{broken"))
        self.assertIn("object", self.refuse(b"[]"))
        self.assertIn("unexpected", self.refuse(dict(REQUEST, force=True)))
        self.assertIn("fields", self.refuse(dict(REQUEST, fields="all")))
        self.assertIn("actor", self.refuse(dict(REQUEST, actor=7)))
        self.assertIn("dryRun", self.refuse(dict(REQUEST, dryRun="yes")))
        self.assertIn("non-empty", self.refuse(dict(REQUEST, id="   ")))

    def test_an_oversized_request_is_refused_without_parsing(self):
        self.assertIn("too large", self.refuse(b"x" * (CW.MAX_REQUEST_BYTES + 1)))


class WriteEnvironment(unittest.TestCase):
    def test_the_write_key_becomes_the_key_the_writer_uses(self):
        env = CW.write_env({CW.WRITE_KEY_VAR: KEY, "GCP_SERVICE_ACCOUNT_B64": "runtime"})
        self.assertEqual(env["GCP_SERVICE_ACCOUNT_B64"], KEY)

    def test_a_file_key_cannot_shadow_the_write_key(self):
        env = CW.write_env({CW.WRITE_KEY_VAR: KEY,
                            "GCP_SERVICE_ACCOUNT_FILE": "/tmp/seed-key.json"})
        self.assertNotIn("GCP_SERVICE_ACCOUNT_FILE", env)

    def test_no_write_key_means_no_write_rather_than_the_runtime_key(self):
        # The runtime key is Cloud Datastore Viewer, so falling back to it would
        # fail at the commit -- but only after reading, and with a worse message.
        with self.assertRaises(operator_writer.WriteRefused) as caught:
            CW.write_env({"GCP_SERVICE_ACCOUNT_B64": "runtime"})
        self.assertEqual(caught.exception.status, 503)
        self.assertIn(CW.WRITE_KEY_VAR, str(caught.exception))

    def test_a_blank_write_key_is_no_key(self):
        with self.assertRaises(operator_writer.WriteRefused):
            CW.write_env({CW.WRITE_KEY_VAR: "   "})

    def test_the_token_cache_is_separate_from_the_read_path(self):
        # access_token keys its cache by client_email, so one shared file would
        # make each side evict the other's token and mint a fresh one per write.
        env = {CW.WRITE_KEY_VAR: KEY, "OPENCLAW_STATE_DIR": "/vol"}
        written = CW.write_env(env)
        self.assertNotEqual(operator_config.state_dir(written),
                            operator_config.state_dir(env))
        self.assertTrue(operator_config.state_dir(written).startswith(
            operator_config.state_dir(env)))

    def test_the_writer_keeps_no_snapshot_of_its_own(self):
        # The writer's directory holds only a token cache: operator_config.load
        # is what writes the fail-closed snapshot, and the writer uses fetch, so
        # nothing a campaign run depends on lives under there. Asserted against
        # the source because the difference is which function is called.
        source = (ROOT / "workspace/lib/operator_writer.py").read_text(encoding="utf-8")
        self.assertIn("operator_config.fetch(", source)
        self.assertNotIn("operator_config.load(", source)


class Payload(unittest.TestCase):
    def run_main(self, raw=None, env=None, **patch):
        out = io.StringIO()
        with unittest.mock.patch.object(operator_writer, "write", **patch):
            code = CW.main([], stdin=io.BytesIO(
                raw if raw is not None else json.dumps(REQUEST).encode()),
                stdout=out, env=env or {CW.WRITE_KEY_VAR: KEY})
        return code, json.loads(out.getvalue())

    def test_an_applied_change_is_reported_with_a_status_the_api_accepts(self):
        applied = {"collection": "groups", "id": "x@g.us", "change": "updated",
                   "fields": ["status"], "before": {}, "after": {},
                   "warnings": [], "audit": "20261106T000000Z-abcd"}
        code, payload = self.run_main(return_value=applied)
        self.assertEqual(code, 0)
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["status"], 200)
        self.assertEqual(payload["audit"], applied["audit"])

    def test_a_refusal_carries_its_status_and_reasons(self):
        code, payload = self.run_main(side_effect=operator_writer.WriteRefused(
            "this change would stop it being advertised to",
            problems=["group x: unknown status 'paused'"], status=400))
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], 400)
        self.assertTrue(payload["problems"])

    def test_an_unreadable_config_means_nothing_was_changed(self):
        # A write must never be applied to a guess at the current state, and the
        # message has to say that the change did not happen.
        code, payload = self.run_main(
            side_effect=operator_config.ConfigUnavailable("firestore unreachable"))
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], 503)
        self.assertIn("nothing was changed", payload["error"])

    def test_a_broken_stored_config_is_a_conflict_not_a_crash(self):
        code, payload = self.run_main(
            side_effect=operator_config.ConfigInvalid("settings/defaults is missing"))
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], 409)

    def test_every_answer_carries_ok_and_status_that_agree(self):
        # The API refuses to pass on an answer whose ok and status disagree.
        for kwargs in ({"return_value": {"change": "unchanged", "fields": [],
                                         "before": {}, "after": {}, "warnings": [],
                                         "audit": None, "collection": "groups",
                                         "id": "x@g.us"}},
                       {"side_effect": operator_writer.WriteRefused("no")}):
            with self.subTest(kwargs=sorted(kwargs)):
                code, payload = self.run_main(**kwargs)
                self.assertIs(payload["ok"], payload["status"] == 200)
                self.assertEqual(code, 0 if payload["ok"] else 1)

    def test_a_bad_request_never_reaches_the_writer(self):
        with unittest.mock.patch.object(operator_writer, "write") as write:
            out = io.StringIO()
            code = CW.main([], stdin=io.BytesIO(b"{broken"), stdout=out,
                           env={CW.WRITE_KEY_VAR: KEY})
        self.assertEqual(code, 1)
        self.assertFalse(write.called)
        self.assertEqual(json.loads(out.getvalue())["status"], 400)


if __name__ == "__main__":
    unittest.main()
