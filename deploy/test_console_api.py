"""Cover the console API's refusals, its limits and its secret hygiene.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes them.

Every case drives a real socket against a real server, but the bridge is a
stand-in script in a temporary directory, so these tests need no WhatsApp state,
no network and no genaebot checkout.
"""

import importlib.util
import json
import os
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "console_api", ROOT / "deploy/railway/console-api.py")
API = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(API)

TOKEN = "t" * API.MIN_TOKEN_LENGTH
GATE = "g" * API.MIN_TOKEN_LENGTH

# Echoes its own arguments, so a response proves which bridge call a route made.
ECHO = """import json, sys
json.dump({"args": sys.argv[1:]}, sys.stdout)
sys.stdout.write("\\n")
"""

# Exits like console-read does for something the caller named that is not there.
ABSENT = """import json, sys
json.dump({"error": "unknown campaign 'nope'"}, sys.stdout)
sys.exit(3)
"""

# A curated failure of our own: the message is safe, the exit code is not 3.
CURATED = """import json, sys
json.dump({"error": "membership file not found"}, sys.stdout)
sys.exit(1)
"""

# Crashes the way an unhandled exception would, with detail only on stderr.
CRASHED = """import sys
sys.stderr.write("Traceback: /home/joseph/.openclaw/secret-path GATEWAY_AUTH_TOKEN=hunter2\\n")
sys.exit(1)
"""

SLOW = """import time
time.sleep(30)
"""


class ConsoleApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.echo = self.write("echo.py", ECHO)
        # The server logs every request to stdout, which is the point in the
        # container and only noise here.
        self.logged = []
        patched = unittest.mock.patch.object(API, "log", self.logged.append)
        patched.start()
        self.addCleanup(patched.stop)

    def write(self, name, source):
        path = Path(self.directory.name) / name
        path.write_text(source, encoding="utf-8")
        return str(path)

    def serve(self, bridge=None, token=TOKEN, **kwargs):
        """A live server on an ephemeral port; returns its base URL."""
        server = API.build_server("127.0.0.1", 0, token, bridge or self.echo, **kwargs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # Reverse registration order, so this unwinds shutdown, join, close.
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        self.server = server
        return "http://127.0.0.1:%d/internal/console" % server.server_address[1]

    def fetch(self, url, token=TOKEN, method="GET", data=None):
        request = urllib.request.Request(url, data=data, method=method)
        if token is not None:
            request.add_header("Authorization", "Bearer %s" % token)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read(), dict(error.headers)

    def test_live_routes_are_authenticated_uncached_and_do_not_run_legacy_bridge(self):
        base = self.serve()
        live = unittest.mock.Mock()
        live.handle.return_value = {"rows": [], "sync": {"stale": False}, "revision": "1"}
        self.server.console["live"] = live
        self.assertEqual(self.fetch(base + "/live/groups", token=None)[0], 401)
        for _ in range(2):
            status, body, headers = self.fetch(base + "/live/groups?q=market")
            self.assertEqual(status, 200)
            self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(live.handle.call_count, 2)
        self.assertEqual(live.handle.call_args.args, ("groups", "q=market"))
        self.assertEqual(self.server.console["bridge"].runs, 0)
        live.handle.side_effect = ValueError("bad cursor")
        self.assertEqual(self.fetch(base + "/live/groups?cursor=bad")[0], 400)
        live.handle.side_effect = RuntimeError("private detail")
        status, body, _ = self.fetch(base + "/live/groups")
        self.assertEqual(status, 503)
        self.assertNotIn(b"private detail", body)

    def test_account_routes_use_separate_uncached_controls(self):
        base = self.serve()
        controls = self.server.console["accounts"]
        with unittest.mock.patch.object(controls, "run", return_value=(200, b'{"accounts":[]}')) as run:
            for _ in range(2):
                self.assertEqual(self.fetch(base + "/accounts")[0], 200)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.args, ({"action": "list"},))
            self.assertEqual(self.server.console["bridge"].runs, 0)

    def test_account_mutations_validate_auth_body_and_route(self):
        base = self.serve()
        controls = self.server.console["accounts"]
        def post(route, payload, token=TOKEN):
            request = urllib.request.Request(base + route, data=payload,
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + token}, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status
            except urllib.error.HTTPError as error:
                with error:
                    return error.code
        with unittest.mock.patch.object(controls, "run", return_value=(200, b'{"ok":true}')) as run:
            self.assertEqual(post("/accounts/ads/pause", b"{}", "wrong"), 401)
            self.assertEqual(post("/accounts/ads/pause", b"{broken"), 400)
            self.assertEqual(post("/accounts/ads/pause", b'{"force":true}'), 404)
            self.assertEqual(post("/accounts/ads/remove", b"{}"), 404)
            self.assertEqual(post("/accounts", b'{"id":123}'), 400)
            self.assertEqual(post("/accounts", b'{"id":["sales"]}'), 400)
            self.assertFalse(run.called)
            self.assertEqual(post("/accounts", b'{"id":"sales"}'), 200)
            self.assertEqual(run.call_args.args, ({"action": "link", "accountId": "sales", "create": True},))
            self.assertEqual(post("/accounts/ads/pause", b"{}"), 200)
            self.assertEqual(run.call_args.args, ({"action": "pause", "accountId": "ads"},))

    # -- the release gates' read-scoped token -----------------------------

    def test_gate_token_reads_campaigns_and_nothing_else(self):
        """CI holds this token, so what it cannot reach matters more than what it can."""
        base = self.serve(gate_token=GATE)
        self.assertEqual(self.fetch(base + "/campaigns", token=GATE)[0], 200)
        self.assertEqual(self.fetch(base + "/campaigns?date=2026-11-11", token=GATE)[0], 200)
        for route in ("/overview", "/groups", "/settings", "/navigation",
                      "/preview/genaie", "/accounts", "/accounts/link-sessions/x"):
            self.assertEqual(self.fetch(base + route, token=GATE)[0], 401,
                             "%s must refuse the gate token" % route)

    def test_gate_token_cannot_reach_the_live_views_either(self):
        base = self.serve(gate_token=GATE)
        live = unittest.mock.Mock()
        live.handle.return_value = {"rows": [], "sync": {"stale": False}, "revision": "1"}
        self.server.console["live"] = live
        self.assertEqual(self.fetch(base + "/live/campaigns", token=GATE)[0], 401)
        self.assertFalse(live.handle.called)

    def test_gate_token_writes_nothing(self):
        base = self.serve(gate_token=GATE)
        writes, actions = self.server.console["writes"], self.server.console["actions"]
        with unittest.mock.patch.object(writes, "run") as write, \
                unittest.mock.patch.object(actions, "run") as action:
            for route, payload in (("/config/campaigns/genaie", b'{"fields":{"enabled":false}}'),
                                   ("/config/groups/1@g.us", b'{"delete":true}'),
                                   ("/campaigns/genaie/delete", b"{}"),
                                   ("/campaigns/create", b"{}"),
                                   ("/groups/1@g.us/leave", b"{}"),
                                   ("/accounts/ads/pause", b"{}")):
                request = urllib.request.Request(
                    base + route, data=payload, method="POST",
                    headers={"Content-Type": "application/json",
                             "Authorization": "Bearer %s" % GATE})
                try:
                    with urllib.request.urlopen(request, timeout=10) as reply:
                        status = reply.status
                except urllib.error.HTTPError as error:
                    with error:
                        status = error.code
                self.assertEqual(status, 401, "%s must refuse the gate token" % route)
            self.assertFalse(write.called)
            self.assertFalse(action.called)

    def test_the_operator_token_is_unaffected_and_absent_gate_changes_nothing(self):
        base = self.serve(gate_token=GATE)
        for route in ("/campaigns", "/overview", "/groups"):
            self.assertEqual(self.fetch(base + route)[0], 200)
        plain = self.serve()
        self.assertEqual(self.fetch(plain + "/campaigns")[0], 200)
        self.assertEqual(self.fetch(plain + "/campaigns", token=GATE)[0], 401)
        self.assertEqual(self.fetch(plain + "/health", token=None)[0], 200)

    def test_refuses_a_gate_token_that_is_short_or_the_operator_token(self):
        for gate in ("short", "g" * (API.MIN_TOKEN_LENGTH - 1), TOKEN):
            with self.assertRaises(ValueError):
                API.build_server("127.0.0.1", 0, TOKEN, self.echo, gate_token=gate)

    # -- starting up ------------------------------------------------------

    def test_refuses_to_start_without_a_long_enough_token(self):
        for token in (None, "", "short", "t" * (API.MIN_TOKEN_LENGTH - 1)):
            with self.assertRaises(ValueError):
                API.build_server("127.0.0.1", 0, token, self.echo)

    def test_starts_with_a_token_of_exactly_the_minimum_length(self):
        base = self.serve(token="t" * API.MIN_TOKEN_LENGTH)
        self.assertEqual(self.fetch(base + "/groups")[0], 200)

    # -- authentication ---------------------------------------------------

    def test_health_needs_no_token_and_reveals_nothing_else(self):
        base = self.serve()
        status, body, _ = self.fetch(base + "/health", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})

    def test_every_way_of_failing_to_authenticate_is_401(self):
        base = self.serve()
        cases = [None, "", "wrong", "T" * API.MIN_TOKEN_LENGTH, TOKEN + "x", TOKEN[:-1]]
        for token in cases:
            status, body, headers = self.fetch(base + "/groups", token=token)
            self.assertEqual(status, 401, token)
            self.assertEqual(json.loads(body), {"error": "unauthorized"})
            self.assertEqual(headers.get("WWW-Authenticate"), "Bearer")

    def test_a_non_bearer_scheme_is_refused_even_with_the_right_secret(self):
        base = self.serve()
        request = urllib.request.Request(base + "/groups")
        request.add_header("Authorization", "Basic %s" % TOKEN)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=30)
        with caught.exception as response:
            self.assertEqual(response.code, 401)

    # -- routing ----------------------------------------------------------

    def test_each_route_calls_the_bridge_it_should(self):
        base = self.serve()
        expected = {
            "/overview": ["overview"],
            "/groups": ["groups"],
            "/campaigns": ["campaigns"],
            "/preview/borehole-drilling": ["preview", "borehole-drilling"],
            "/groups?date=2026-11-04": ["groups", "--date", "2026-11-04"],
            "/preview/cv?date=2026-01-02": ["preview", "cv", "--date", "2026-01-02"],
        }
        for route, args in expected.items():
            status, body, _ = self.fetch(base + route)
            self.assertEqual(status, 200, route)
            self.assertEqual(json.loads(body), {"args": args}, route)

    def test_unknown_paths_and_unusable_slugs_are_404(self):
        base = self.serve()
        root = base.rsplit("/internal/", 1)[0]
        for route in ("/frobnicate", "/preview", "/preview/CV", "/preview/a_b",
                      "/preview/%2e%2e%2fetc%2fpasswd", "/preview/" + "a" * 65,
                      "/preview/x/y", "/groups/extra", "/health/x"):
            status, body, _ = self.fetch(base + route)
            self.assertEqual(status, 404, route)
            self.assertEqual(json.loads(body), {"error": "not found"}, route)
        self.assertEqual(self.fetch(root + "/")[0], 404)

    def test_a_slug_at_the_length_limit_is_still_accepted(self):
        base = self.serve()
        status, body, _ = self.fetch(base + "/preview/" + "a" * 64)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["args"], ["preview", "a" * 64])

    def test_only_a_well_formed_date_is_accepted(self):
        base = self.serve()
        for query in ("date=nope", "date=", "date=2026-1-1", "date=20260101",
                      "date=2026-11-04&date=2026-11-05"):
            status, body, _ = self.fetch(base + "/groups?" + query)
            self.assertEqual(status, 400, query)
            self.assertIn("date", json.loads(body)["error"])

    def test_an_unrecognized_query_parameter_is_refused(self):
        base = self.serve()
        status, body, _ = self.fetch(base + "/groups?limit=5")
        self.assertEqual(status, 400)
        self.assertIn("limit", json.loads(body)["error"])

    # -- methods and bodies -----------------------------------------------

    def test_anything_that_could_write_is_refused(self):
        base = self.serve()
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            status, body, headers = self.fetch(base + "/groups", method=method)
            self.assertEqual(status, 405, method)
            self.assertEqual(json.loads(body), {"error": "method not allowed"})
            self.assertEqual(headers.get("Allow"), "GET, HEAD")

    def test_a_request_body_is_refused_rather_than_read(self):
        base = self.serve()
        status, body, _ = self.fetch(base + "/groups", method="GET", data=b"payload")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": "unexpected request body"})

    def test_head_answers_with_headers_and_no_body(self):
        base = self.serve()
        status, body, headers = self.fetch(base + "/groups", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(int(headers["Content-Length"]),
                         len(json.dumps({"args": ["groups"]})) + 1)

    # -- bridge failure ---------------------------------------------------

    def test_something_the_caller_named_that_is_absent_is_404_not_502(self):
        base = self.serve(bridge=self.write("absent.py", ABSENT))
        status, body, _ = self.fetch(base + "/preview/nope")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "unknown campaign 'nope'"})

    def test_our_own_curated_failure_is_passed_through(self):
        base = self.serve(bridge=self.write("curated.py", CURATED))
        status, body, _ = self.fetch(base + "/groups")
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body), {"error": "membership file not found"})

    def test_a_crash_is_502_and_stderr_never_reaches_the_caller(self):
        base = self.serve(bridge=self.write("crashed.py", CRASHED))
        status, body, _ = self.fetch(base + "/groups")
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body), {"error": "bridge failed"})
        for leaked in (b"Traceback", b"secret-path", b"hunter2", b"GATEWAY_AUTH_TOKEN"):
            self.assertNotIn(leaked, body)
        # It must still be diagnosable from the container log.
        self.assertTrue(any("secret-path" in line for line in self.logged))

    def test_a_missing_bridge_is_502_and_does_not_stop_the_server(self):
        base = self.serve(bridge=os.path.join(self.directory.name, "absent-file.py"))
        self.assertEqual(self.fetch(base + "/groups")[0], 502)
        self.assertEqual(self.fetch(base + "/health", token=None)[0], 200)

    def test_a_bridge_that_hangs_is_cut_off(self):
        base = self.serve(bridge=self.write("slow.py", SLOW), timeout=0.2)
        status, body, _ = self.fetch(base + "/groups")
        self.assertEqual(status, 504)
        self.assertEqual(json.loads(body), {"error": "bridge timed out"})

    # -- the two limits that protect the gateway --------------------------

    def test_repeating_a_request_runs_the_bridge_once(self):
        base = self.serve()
        for _ in range(5):
            self.assertEqual(self.fetch(base + "/groups?date=2026-11-04")[0], 200)
        self.assertEqual(self.server.console["bridge"].runs, 1)

    def test_a_different_date_is_a_different_cache_entry(self):
        base = self.serve()
        self.fetch(base + "/groups?date=2026-11-04")
        self.fetch(base + "/groups?date=2026-11-05")
        self.fetch(base + "/groups")
        self.assertEqual(self.server.console["bridge"].runs, 3)

    def test_an_expired_entry_is_not_served(self):
        base = self.serve(ttl=0)
        self.fetch(base + "/groups")
        self.fetch(base + "/groups")
        self.assertEqual(self.server.console["bridge"].runs, 2)

    def test_failures_are_never_cached(self):
        base = self.serve(bridge=self.write("crashed.py", CRASHED))
        self.fetch(base + "/groups")
        self.fetch(base + "/groups")
        self.assertEqual(self.server.console["bridge"].runs, 2)

    def test_requests_beyond_the_concurrency_cap_are_refused_immediately(self):
        base = self.serve(concurrency=1)
        # Holding the only slot is what a request in flight looks like, without
        # needing a second thread or a sleep to arrange it.
        self.assertTrue(self.server.console["bridge"].slots.acquire(blocking=False))
        try:
            status, body, headers = self.fetch(base + "/groups")
        finally:
            self.server.console["bridge"].slots.release()
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body), {"error": "busy"})
        self.assertEqual(headers.get("Retry-After"), "1")
        # The cap sheds load; it does not break the server.
        self.assertEqual(self.fetch(base + "/groups")[0], 200)

    def test_a_cached_answer_is_served_even_while_every_slot_is_busy(self):
        bridge = API.Bridge(self.echo, concurrency=1)
        status, body = bridge.run(["groups"])
        self.assertEqual(status, 200)
        bridge.slots.acquire()
        try:
            self.assertEqual(bridge.run(["groups"]), (200, body))
        finally:
            bridge.slots.release()

    def test_the_cache_cannot_grow_without_bound(self):
        bridge = API.Bridge(self.echo)
        for index in range(API.CACHE_MAX_ENTRIES * 4):
            bridge.store(("preview", "campaign-%d" % index), b"{}")
        self.assertLessEqual(len(bridge.cache), API.CACHE_MAX_ENTRIES)

    def test_an_expired_entry_is_evicted_rather_than_counted_as_live(self):
        bridge = API.Bridge(self.echo, ttl=0)
        bridge.store(("groups",), b"{}")
        self.assertIsNone(bridge.cached(("groups",)))
        self.assertEqual(bridge.cache, {})

    # -- unit level -------------------------------------------------------

    def test_fresh_query_bypasses_response_cache_every_time(self):
        base = self.serve()
        self.fetch(base + "/groups")
        for _ in range(2):
            status, body, _ = self.fetch(base + "/groups?date=2026-11-09&fresh=1")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["args"], ["groups", "--date", "2026-11-09", "--fresh-config"])
        self.assertEqual(self.server.console["bridge"].runs, 3)
        self.fetch(base + "/groups")
        self.assertEqual(self.server.console["bridge"].runs, 3)
        for query in ("fresh=0", "fresh=", "fresh=1&fresh=1"):
            self.assertEqual(self.fetch(base + "/groups?" + query)[0], 400)

    def test_an_older_inflight_read_cannot_repopulate_invalidated_response_cache(self):
        bridge = API.Bridge(self.echo)
        started, finish = threading.Event(), threading.Event()
        def run(*args, **kw):
            started.set()
            self.assertTrue(finish.wait(5))
            return type("Result", (), {"returncode": 0, "stdout": b"old"})()
        with unittest.mock.patch.object(API.subprocess, "run", run):
            reader = threading.Thread(target=bridge.run, args=(["groups"],))
            reader.start()
            self.assertTrue(started.wait(5))
            bridge.invalidate()
            finish.set()
            reader.join(5)
        self.assertEqual(bridge.cache, {})

    def test_response_only_invalidation_preserves_config_generation(self):
        bridge = API.Bridge(self.echo)
        generation = bridge.config_generation
        bridge.invalidate(config=False)
        self.assertEqual(bridge.config_generation, generation)
        bridge.invalidate()
        self.assertNotEqual(bridge.config_generation, generation)

    def test_structured_config_error_survives_the_bridge(self):
        source = 'import json,sys\njson.dump({"error":"config unavailable","code":"config_rate_limited","upstreamStatus":429,"quota":"daily"},sys.stdout)\nsys.exit(1)'
        base = self.serve(bridge=self.write("limited.py", source))
        status, body, _ = self.fetch(base + "/groups?fresh=1")
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["code"], "config_rate_limited")

    def test_curated_only_accepts_a_lone_string_error(self):
        self.assertTrue(API.curated(b'{"error": "nope"}'))
        for payload in (b"", b"not json", b"[]", b'{"error": 1}',
                        b'{"error": "a", "extra": 2}', b'{"ok": true}', b"null"):
            self.assertFalse(API.curated(payload), payload)

    def test_the_default_bridge_path_follows_the_state_directory(self):
        previous = os.environ.get("OPENCLAW_STATE_DIR")
        os.environ["OPENCLAW_STATE_DIR"] = "/somewhere"
        try:
            self.assertEqual(API.default_bridge(),
                             "/somewhere/workspace/bin/console-read")
        finally:
            if previous is None:
                del os.environ["OPENCLAW_STATE_DIR"]
            else:
                os.environ["OPENCLAW_STATE_DIR"] = previous

    def test_plan_rejects_everything_outside_the_four_routes(self):
        self.assertEqual(API.plan("groups", ""), ["groups"])
        for route in ("", "health", "preview", "groups/x", "Groups", "preview/A"):
            with self.assertRaises(API.Refuse):
                API.plan(route, "")


if __name__ == "__main__":
    unittest.main()
