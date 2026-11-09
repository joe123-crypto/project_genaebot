"""The console API's group action route.

What the API itself owns: the token, the shape of a request, the switch that
gates the surface, and never passing on an answer it does not understand. What a
leave actually does belongs to workspace/bin/console-group-action and the
monitor's helper, tested in deploy/test_console_group_action.py and
deploy/railway/whatsapp-group-actions.test.mjs.

The gate is the point of several of these. Leaving a group writes no Firestore
document, so this route must work with no write key configured -- hiding it
behind a credential it does not use would be a bug an operator could not
diagnose.

Every case drives a real socket against a real server with a stand-in bridge, so
these need no gateway, no key and no network.

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

ECHO_ACTION = """import json, sys
request = json.load(sys.stdin)
json.dump({"ok": True, "status": 200, "echo": request}, sys.stdout)
"""

UNCONFIRMED = """import json, sys
sys.stdin.read()
json.dump({"ok": True, "status": 200, "jid": "x", "account": "ads",
           "leave": {"requested": True, "ok": None, "already": False,
                     "error": "unconfirmed"},
           "cleared": {"requested": False}}, sys.stdout)
"""

BAD_STATUS = """import json, sys
sys.stdin.read()
json.dump({"ok": False, "status": 418, "error": "nope"}, sys.stdout)
sys.exit(1)
"""

CRASHED = """import sys
sys.stderr.write("Traceback: /home/joseph/.openclaw CONSOLE_API_TOKEN=secret\\n")
sys.exit(1)
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
        # Nothing here needs the Firestore write key, and several cases prove
        # the route works without one.
        cleared = unittest.mock.patch.dict(os.environ, {}, clear=False)
        cleared.start()
        self.addCleanup(cleared.stop)
        os.environ.pop(API.WRITE_KEY_VAR, None)
        os.environ.pop(API.GROUP_ACTIONS_VAR, None)

    def switch(self, value):
        patched = unittest.mock.patch.dict(
            os.environ, {API.GROUP_ACTIONS_VAR: value}, clear=False)
        patched.start()
        self.addCleanup(patched.stop)

    def script(self, name, source):
        path = Path(self.directory.name) / name
        path.write_text(source, encoding="utf-8")
        return str(path)

    def serve(self, action_source=ECHO_ACTION, token=TOKEN):
        read_bridge = self.script("read.py", READ_ECHO)
        write_bridge = self.script("write.py", ECHO_ACTION)
        action_bridge = self.script("action.py", action_source)
        server = API.build_server("127.0.0.1", 0, token, read_bridge, write_bridge,
                                  action_bridge)
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

    def route(self, doc_id=JID):
        return "/groups/%s/actions" % urllib.parse.quote(doc_id, safe="")


class GroupActionRouteTests(Harness):
    def test_a_request_reaches_the_bridge_as_sent(self):
        base = self.serve()
        status, body = self.post(base, self.route(),
                                 {"leave": True, "clearRecords": True,
                                  "account": "ads", "actor": "joe"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["echo"],
                         {"jid": JID, "leave": True, "clearRecords": True,
                          "account": "ads", "actor": "joe"})

    def test_it_works_with_no_firestore_write_key_configured(self):
        # Leaving a group writes no document; gating it on the write key would
        # hide a working control behind a credential it does not use.
        self.assertNotIn(API.WRITE_KEY_VAR, os.environ)
        base = self.serve()
        status, _ = self.post(base, self.route(), {"leave": True})
        self.assertEqual(status, 200)

    def test_the_switch_turns_the_surface_off_and_says_so(self):
        self.switch("0")
        base = self.serve()
        status, body = self.post(base, self.route(), {"leave": True})
        self.assertEqual(status, 503)
        self.assertIn("switched off", json.loads(body)["error"])

    def test_an_unconfirmed_leave_is_passed_on_as_success(self):
        # It is not a failure: the request is queued and the account may well
        # be out already. An operator who is told "failed" retries it.
        base = self.serve(UNCONFIRMED)
        status, body = self.post(base, self.route(), {"leave": True})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIsNone(payload["leave"]["ok"])
        self.assertEqual(payload["leave"]["error"], "unconfirmed")

    # -- the shape of a request ---------------------------------------------

    def test_a_request_that_asks_for_nothing_is_refused_before_the_bridge(self):
        base = self.serve()
        for payload in ({}, {"leave": False}, {"clearRecords": False, "actor": "joe"}):
            status, _ = self.post(base, self.route(), payload)
            self.assertEqual(status, 400, payload)
        self.assertEqual(self.server.console["actions"].runs, 0)

    def test_unknown_keys_and_wrong_types_are_refused(self):
        base = self.serve()
        for payload in ({"leave": True, "fields": {}}, {"leave": "yes"},
                        {"leave": True, "clearRecords": "no"},
                        {"delete": True}):
            status, _ = self.post(base, self.route(), payload)
            self.assertEqual(status, 400, payload)
        self.assertEqual(self.server.console["actions"].runs, 0)

    def test_a_jid_shaped_nothing_like_one_is_not_found(self):
        base = self.serve()
        for doc_id in ("", "../../etc/passwd", "a" * 200):
            status, _ = self.post(base, self.route(doc_id), {"leave": True})
            self.assertIn(status, (400, 404), doc_id)
        self.assertEqual(self.server.console["actions"].runs, 0)

    def test_the_route_has_to_be_the_whole_route(self):
        base = self.serve()
        for route in ("/groups/%s" % urllib.parse.quote(JID, safe=""),
                      "/groups/%s/leave" % urllib.parse.quote(JID, safe=""),
                      "/groups//actions"):
            status, _ = self.post(base, route, {"leave": True})
            self.assertEqual(status, 404, route)

    def test_a_query_string_is_refused(self):
        base = self.serve()
        status, _ = self.post(base, self.route() + "?date=2026-11-09", {"leave": True})
        self.assertEqual(status, 400)

    def test_a_body_that_is_not_bounded_json_is_refused(self):
        base = self.serve()
        self.assertEqual(self.post(base, self.route(), b"{")[0], 400)
        self.assertEqual(self.post(base, self.route(), {"leave": True},
                                   content_type="text/plain")[0], 400)
        self.assertEqual(self.post(base, self.route(),
                                   json.dumps({"leave": True, "actor": "x" * 4000}).encode())[0],
                         400)
        self.assertEqual(self.server.console["actions"].runs, 0)

    # -- the token ----------------------------------------------------------

    def test_without_the_token_nothing_runs(self):
        base = self.serve()
        for token in (None, "wrong"):
            status, _ = self.post(base, self.route(), {"leave": True}, token=token)
            self.assertEqual(status, 401, token)
        self.assertEqual(self.server.console["actions"].runs, 0)

    # -- answers the API will not pass on -----------------------------------

    def test_a_status_the_bridge_may_not_choose_becomes_a_bad_gateway(self):
        base = self.serve(BAD_STATUS)
        status, body = self.post(base, self.route(), {"leave": True})
        self.assertEqual(status, 502)
        self.assertEqual(set(json.loads(body)), {"error"})

    def test_a_crash_never_reaches_the_caller(self):
        base = self.serve(CRASHED)
        status, body = self.post(base, self.route(), {"leave": True})
        self.assertEqual(status, 502)
        self.assertNotIn(b"Traceback", body)
        self.assertNotIn(b"secret", body)
        # It is still diagnosable from the container log, named by its group.
        self.assertTrue(any("groups/%s" % JID in line for line in self.logged))

    def test_the_read_cache_is_cleared_so_the_groups_list_catches_up(self):
        base = self.serve()
        bridge = self.server.console["bridge"]
        with bridge.lock:
            bridge.cache["groups"] = ("stale", 0)
        self.post(base, self.route(), {"leave": True})
        self.assertEqual(bridge.cache, {})

    def test_other_methods_are_still_refused(self):
        base = self.serve()
        request = urllib.request.Request(
            base + self.route(), headers={"Authorization": "Bearer %s" % TOKEN},
            method="DELETE")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            with error:
                status = error.code
        self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main()
