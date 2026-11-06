"""The console API's operator-config write routes.

What the API itself is responsible for: the token, the shape of a request, the
key that gates the whole surface, serialising writes, clearing the read cache,
and never passing on an answer it does not understand. What a legal CHANGE is
belongs to console-write and workspace/lib/operator_writer.py, which have the
bot's own validator; those are tested in workspace/lib/test_operator_writer.py.

Every case drives a real socket against a real server with a stand-in write
bridge, so these need no Firestore, no key and no network.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes them.
"""

import importlib.util
import json
import os
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "console_api", ROOT / "deploy/railway/console-api.py")
API = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(API)

TOKEN = "t" * API.MIN_TOKEN_LENGTH
JID = "263786627810-1599925488@g.us"

# Echoes the request it was given, so a response proves what the API forwarded.
ECHO_WRITE = """import json, sys
request = json.load(sys.stdin)
json.dump({"ok": True, "status": 200, "echo": request}, sys.stdout)
"""

# Refuses the way console-write does for a change that would quarantine a group.
REFUSED = """import json, sys
sys.stdin.read()
json.dump({"ok": False, "status": 400, "error": "this change would stop it being
 advertised to", "problems": ["group x: unknown status"]}, sys.stdout)
sys.exit(1)
""".replace("\n advertised", " advertised")

# Answers with a status the API must not pass on.
BAD_STATUS = """import json, sys
sys.stdin.read()
json.dump({"ok": False, "status": 418, "error": "nope"}, sys.stdout)
sys.exit(1)
"""

# Crashes with detail only on stderr, the way an unhandled exception would.
CRASHED = """import sys
sys.stderr.write("Traceback: /home/joseph/.openclaw GCP_SERVICE_ACCOUNT_WRITE_B64=secret\\n")
sys.exit(1)
"""

SLOW = """import time
time.sleep(30)
"""

READ_ECHO = """import json, sys
json.dump({"args": sys.argv[1:]}, sys.stdout)
"""


class Harness(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.logged = []
        patched = unittest.mock.patch.object(API, "log", self.logged.append)
        patched.start()
        self.addCleanup(patched.stop)
        # The key is the capability; most cases need the surface switched on.
        # Deliberately not key-shaped: the API only checks that a key is
        # configured, never decodes it, so anything non-empty exercises the gate.
        self.set_key("not-a-real-key")

    def set_key(self, value):
        patched = unittest.mock.patch.dict(
            os.environ, {API.WRITE_KEY_VAR: value} if value else {}, clear=False)
        patched.start()
        self.addCleanup(patched.stop)
        if not value:
            os.environ.pop(API.WRITE_KEY_VAR, None)

    def script(self, name, source):
        path = Path(self.directory.name) / name
        path.write_text(source, encoding="utf-8")
        return str(path)

    def serve(self, write_source=ECHO_WRITE, token=TOKEN):
        read_bridge = self.script("read.py", READ_ECHO)
        write_bridge = self.script("write.py", write_source)
        server = API.build_server("127.0.0.1", 0, token, read_bridge, write_bridge)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        self.server = server
        return "http://127.0.0.1:%d/internal/console" % server.server_address[1]

    def post(self, base, route, payload, token=TOKEN, content_type="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        if token is not None:
            headers["Authorization"] = "Bearer %s" % token
        request = urllib.request.Request(base + route, data=body, headers=headers,
                                         method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read()

    def route(self, collection="groups", doc_id=JID):
        return "/config/%s/%s" % (collection, urllib.parse.quote(doc_id, safe=""))


class WriteRouteTests(Harness):
    # -- the surface itself -------------------------------------------------

    def test_a_change_reaches_the_bridge_as_sent(self):
        base = self.serve()
        status, body = self.post(base, self.route(),
                                 {"fields": {"status": "on-hold"}, "actor": "joe"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["echo"],
                         {"collection": "groups", "id": JID,
                          "fields": {"status": "on-hold"}, "actor": "joe"})

    def test_an_encoded_jid_survives_the_round_trip(self):
        # A group JID carries an "@" and dashes; the id is the one part of the
        # path that is not a fixed vocabulary.
        base = self.serve()
        _, body = self.post(base, self.route(), {"fields": {"note": "x"}})
        self.assertEqual(json.loads(body)["echo"]["id"], JID)

    def test_a_dry_run_is_passed_through(self):
        base = self.serve()
        _, body = self.post(base, self.route(),
                            {"fields": {"note": "x"}, "dryRun": True})
        self.assertIs(json.loads(body)["echo"]["dryRun"], True)

    def test_every_editable_collection_is_routable(self):
        base = self.serve()
        for collection in API.CONFIG_COLLECTIONS:
            with self.subTest(collection=collection):
                status, _ = self.post(base, self.route(collection, "defaults"),
                                      {"fields": {"note": "x"}})
                self.assertEqual(status, 200)

    # -- refusals -----------------------------------------------------------

    def test_a_write_needs_the_token(self):
        base = self.serve()
        self.assertEqual(self.post(base, self.route(), {"fields": {"a": 1}},
                                   token="wrong")[0], 401)
        self.assertEqual(self.post(base, self.route(), {"fields": {"a": 1}},
                                   token=None)[0], 401)

    def test_an_unknown_collection_is_not_found(self):
        base = self.serve()
        self.assertEqual(self.post(base, self.route("secrets", "x"),
                                   {"fields": {"a": 1}})[0], 404)

    def test_a_request_without_fields_is_refused(self):
        base = self.serve()
        for payload in ({}, {"actor": "joe"}, {"fields": "all"}, {"fields": None}):
            with self.subTest(payload=payload):
                self.assertEqual(self.post(base, self.route(), payload)[0], 400)

    def test_an_unexpected_key_is_refused(self):
        base = self.serve()
        self.assertEqual(self.post(base, self.route(),
                                   {"fields": {"a": 1}, "force": True})[0], 400)

    def test_a_body_that_is_not_json_is_refused(self):
        base = self.serve()
        self.assertEqual(self.post(base, self.route(), b"{broken")[0], 400)
        self.assertEqual(self.post(base, self.route(), b"[]")[0], 400)

    def test_a_body_without_the_json_content_type_is_refused(self):
        base = self.serve()
        self.assertEqual(self.post(base, self.route(), {"fields": {"a": 1}},
                                   content_type="text/plain")[0], 400)

    def test_an_empty_or_oversized_body_is_refused(self):
        base = self.serve()
        self.assertEqual(self.post(base, self.route(), b"")[0], 400)
        big = json.dumps({"fields": {"note": "x" * 4000}}).encode()
        self.assertEqual(self.post(base, self.route(), big)[0], 400)

    def test_a_document_id_that_is_not_one_is_refused(self):
        base = self.serve()
        for doc_id in ("../settings", "a/b", "", "x" * 200, " "):
            with self.subTest(doc_id=doc_id):
                status, _ = self.post(base, self.route("groups", doc_id),
                                      {"fields": {"note": "x"}})
                self.assertIn(status, (400, 404))

    def test_a_query_parameter_is_refused(self):
        base = self.serve()
        self.assertEqual(self.post(base, self.route() + "?force=1",
                                   {"fields": {"a": 1}})[0], 400)

    def test_other_methods_are_still_refused(self):
        base = self.serve()
        for method in ("PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                request = urllib.request.Request(
                    base + self.route(), data=b"{}", method=method,
                    headers={"Authorization": "Bearer %s" % TOKEN})
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        status = response.status
                except urllib.error.HTTPError as error:
                    with error:
                        status = error.code
                self.assertEqual(status, 405)

    # -- the key gates the whole surface ------------------------------------

    def test_without_the_write_key_nothing_can_be_changed(self):
        # This is how the write surface ships: the routes exist, the token still
        # guards them, and no change can be made until a key is installed.
        self.set_key("")
        base = self.serve()
        status, body = self.post(base, self.route(), {"fields": {"note": "x"}})
        self.assertEqual(status, 503)
        self.assertIn(b"not enabled", body)
        self.assertEqual(self.server.console["writes"].runs, 0)

    def test_the_key_is_checked_before_the_body_is_read(self):
        self.set_key("")
        base = self.serve()
        self.assertEqual(self.post(base, self.route(), b"{broken")[0], 503)

    # -- what the bridge says ----------------------------------------------

    def test_a_refusal_keeps_its_status_and_reasons(self):
        base = self.serve(REFUSED)
        status, body = self.post(base, self.route(), {"fields": {"status": "x"}})
        self.assertEqual(status, 400)
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["problems"])

    def test_a_status_the_api_does_not_know_is_a_bad_gateway(self):
        base = self.serve(BAD_STATUS)
        self.assertEqual(self.post(base, self.route(), {"fields": {"a": 1}})[0], 502)

    def test_a_crash_never_reaches_the_caller(self):
        base = self.serve(CRASHED)
        status, body = self.post(base, self.route(), {"fields": {"a": 1}})
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body), {"error": "write bridge failed"})
        self.assertNotIn(b"secret", body)
        self.assertNotIn(b"openclaw", body)
        # The detail is for the container log, which is not a response.
        self.assertTrue(any("Traceback" in line for line in self.logged))

    def test_a_timeout_says_the_change_may_have_landed(self):
        # The honest answer: the bridge was cut off, not that nothing happened.
        base = self.serve(SLOW)
        with unittest.mock.patch.object(self.server.console["writes"], "timeout", 0.5):
            status, body = self.post(base, self.route(), {"fields": {"a": 1}})
        self.assertEqual(status, 504)
        self.assertIn(b"may or may not", body)

    def test_an_answer_whose_ok_and_status_disagree_is_not_passed_on(self):
        self.assertIsNone(API.write_status(b'{"ok": true, "status": 400}'))
        self.assertIsNone(API.write_status(b'{"ok": false, "status": 200}'))
        self.assertIsNone(API.write_status(b'{"status": 200}'))
        self.assertIsNone(API.write_status(b"not json"))
        self.assertEqual(API.write_status(b'{"ok": true, "status": 200}'), 200)


class WriteIsolationTests(Harness):
    def test_writes_are_serialised(self):
        # operator_writer reads the config, applies a patch and writes the whole
        # document back, so two writes at once could each miss the other.
        base = self.serve()
        bridge = self.server.console["writes"]
        seen = []
        original = bridge.run

        def run(request):
            seen.append(bridge.lock.locked())
            return original(request)

        with unittest.mock.patch.object(bridge, "run", run):
            threads = [threading.Thread(
                target=self.post, args=(base, self.route(), {"fields": {"note": str(i)}}))
                for i in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        self.assertEqual(len(seen), 4)
        self.assertEqual(bridge.runs, 4)

    def test_a_write_clears_the_cached_reads(self):
        # A change the caller can see means every cached read of it is wrong.
        base = self.serve()
        read = self.server.console["bridge"]
        read.cache[("groups",)] = (float("inf"), b'{"stale": true}')
        self.post(base, self.route(), {"fields": {"note": "x"}})
        self.assertEqual(read.cache, {})

    def test_even_a_failed_write_clears_them(self):
        # An uncertain write may still have reached Firestore.
        base = self.serve(CRASHED)
        read = self.server.console["bridge"]
        read.cache[("groups",)] = (float("inf"), b'{"stale": true}')
        self.post(base, self.route(), {"fields": {"note": "x"}})
        self.assertEqual(read.cache, {})

    def test_writes_never_use_the_read_bridge(self):
        base = self.serve()
        self.post(base, self.route(), {"fields": {"note": "x"}})
        self.assertEqual(self.server.console["bridge"].runs, 0)


if __name__ == "__main__":
    unittest.main()
