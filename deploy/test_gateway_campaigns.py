"""Reading the live campaign list, for the two release gates.

Both gates used to read committed files and now ask the gateway, so the thing
worth pinning is that nothing here ever turns a failed read into an empty or
half-understood answer: a gate that cannot see must say so, because the
alternative is releasing into a campaign's sending window.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes it.
"""

import json
import subprocess
import sys
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "deploy") not in sys.path:
    sys.path.insert(0, str(ROOT / "deploy"))

import gateway_campaigns as gc  # noqa: E402

WRAPPER = (ROOT / "deploy/gateway-campaigns.sh").read_text(encoding="utf-8")


def row(slug, **overrides):
    out = {"slug": slug, "name": slug, "jobName": slug + "-promo", "enabled": True,
           "hour": 9, "minute": 5, "timezone": "Africa/Harare",
           "targetCount": 12, "timeoutSeconds": 7200, "source": "console"}
    out.update(overrides)
    return out


def reply(rows, **extra):
    return json.dumps({"date": "2026-11-11", "campaigns": rows, **extra})


class Completed:
    def __init__(self, code=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = code, stdout, stderr


class ParseTest(unittest.TestCase):
    def test_rows_come_back_as_reported(self):
        rows = gc.parse(reply([row("a"), row("b", enabled=False)]))
        self.assertEqual([entry["slug"] for entry in rows], ["a", "b"])
        self.assertEqual([entry["slug"] for entry in gc.enabled(rows)], ["a"])

    def test_job_names_cover_only_enabled_campaigns(self):
        rows = gc.parse(reply([row("a"), row("b", enabled=False),
                               row("web-design", jobName="console-web-design-promo")]))
        self.assertEqual(gc.job_names(rows), {"a-promo", "console-web-design-promo"})

    def test_an_error_reply_is_unavailable_not_an_empty_list(self):
        # console-read reports a failed read as {"error": ...} with exit 0, so
        # this is the shape most likely to be mistaken for "no campaigns".
        with self.assertRaises(gc.Unavailable) as caught:
            gc.parse(json.dumps({"error": "campaign config unavailable"}))
        self.assertIn("campaign config unavailable", str(caught.exception))

    def test_no_campaigns_at_all_is_unavailable(self):
        with self.assertRaises(gc.Unavailable) as caught:
            gc.parse(reply([]))
        self.assertIn("no campaigns at all", str(caught.exception))

    def test_every_campaign_may_be_disabled(self):
        rows = gc.parse(reply([row("a", enabled=False)]))
        self.assertEqual(gc.enabled(rows), [])
        self.assertEqual(gc.job_names(rows), set())

    def test_a_missing_field_names_itself_and_its_campaign(self):
        for field in gc.REQUIRED:
            incomplete = row("a")
            del incomplete[field]
            with self.assertRaises(gc.Unavailable) as caught:
                gc.parse(reply([incomplete]))
            self.assertIn(field, str(caught.exception))
            if field != "slug":
                self.assertIn("'a'", str(caught.exception))

    def test_a_campaign_the_gateway_could_not_read_is_refused(self):
        with self.assertRaises(gc.Unavailable) as caught:
            gc.parse(reply([row("a", error="ImportError: boom")]))
        self.assertIn("ImportError", str(caught.exception))

    def test_an_enabled_campaign_without_a_job_name_is_refused(self):
        rows = gc.parse(reply([row("a", jobName="")]))
        with self.assertRaises(gc.Unavailable):
            gc.job_names(rows)

    def test_junk_is_not_mistaken_for_an_answer(self):
        for text in ("not json", "[]", '"a string"', "null"):
            with self.assertRaises(gc.Unavailable):
                gc.parse(text)


class RunTest(unittest.TestCase):
    def test_a_successful_run_is_parsed(self):
        rows = gc.read(runner=lambda *a, **k: Completed(stdout=reply([row("a")])))
        self.assertEqual(rows[0]["slug"], "a")

    def test_a_nonzero_exit_reports_the_last_lines_of_its_output(self):
        def runner(*args, **kwargs):
            return Completed(code=7, stderr="connecting...\nno such service\n")
        with self.assertRaises(gc.Unavailable) as caught:
            gc.read(runner=runner)
        self.assertIn("exit 7", str(caught.exception))
        self.assertIn("no such service", str(caught.exception))

    def test_the_reason_survives_a_cli_that_ends_in_advice(self):
        # The Railway CLI says what went wrong and then what to do about it, so
        # the last line alone named neither the project nor the problem.
        def runner(*args, **kwargs):
            return Completed(code=1, stderr="No linked project found\n\n"
                                            "  -> Run `railway link`.\n"
                                            "Then run this command again.\n")
        with self.assertRaises(gc.Unavailable) as caught:
            gc.read(runner=runner)
        self.assertIn("No linked project found", str(caught.exception))

    def test_a_silent_failure_still_says_something(self):
        with self.assertRaises(gc.Unavailable) as caught:
            gc.read(runner=lambda *a, **k: Completed(code=1))
        self.assertIn("no output", str(caught.exception))

    def test_a_timeout_names_the_bound(self):
        def runner(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="x", timeout=180)
        with self.assertRaises(gc.Unavailable) as caught:
            gc.read(runner=runner, timeout=180)
        self.assertIn("180 seconds", str(caught.exception))

    def test_a_missing_wrapper_is_named(self):
        def runner(*args, **kwargs):
            raise FileNotFoundError()
        with self.assertRaises(gc.Unavailable) as caught:
            gc.read(runner=runner, script="/nonexistent/gateway-campaigns.sh")
        self.assertIn("gateway-campaigns.sh", str(caught.exception))


class ConsoleReadTest(unittest.TestCase):
    """How CI reads the list. `railway ssh` wants an SSH key a runner cannot
    have, so CI goes over the console API with a read-scoped token instead."""

    TOKEN = "g" * 32

    class Reply:
        def __init__(self, body):
            self.body = body.encode("utf-8") if isinstance(body, str) else body
        def read(self, limit=None):
            return self.body[:limit] if limit else self.body
        def __enter__(self):
            return self
        def __exit__(self, *rest):
            return False

    def opener(self, body=None, error=None):
        self.seen = {}
        def open_it(request, timeout=None):
            self.seen = {"url": request.full_url, "timeout": timeout,
                         "auth": request.get_header("Authorization")}
            if error is not None:
                raise error
            return self.Reply(body)
        return open_it

    def read(self, **kwargs):
        return gc.read(env={gc.GATE_TOKEN_VAR: self.TOKEN}, **kwargs)

    def test_it_asks_the_console_api_with_a_bearer_token(self):
        rows = self.read(opener=self.opener(reply([row("a")])))
        self.assertEqual(rows[0]["slug"], "a")
        self.assertEqual(self.seen["url"], gc.CONSOLE_URL + gc.READ_PATH)
        self.assertEqual(self.seen["auth"], "Bearer " + self.TOKEN)

    def test_the_base_url_can_be_pointed_elsewhere(self):
        gc.read(env={gc.GATE_TOKEN_VAR: self.TOKEN,
                     gc.CONSOLE_URL_VAR: "https://staging.example.com/"},
                opener=self.opener(reply([row("a")])))
        self.assertEqual(self.seen["url"], "https://staging.example.com" + gc.READ_PATH)

    def test_a_refusal_names_the_status_and_not_the_token(self):
        for code, word in ((401, "token"), (404, "console api"), (502, "502")):
            error = urllib.error.HTTPError(gc.CONSOLE_URL, code, "no", {}, None)
            with self.assertRaises(gc.Unavailable) as caught:
                self.read(opener=self.opener(error=error))
            message = str(caught.exception)
            self.assertIn(str(code), message)
            self.assertIn(word, message.lower())
            self.assertNotIn(self.TOKEN, message)

    def test_an_unreachable_console_api_is_named_without_the_token(self):
        for error in (urllib.error.URLError("no route"), TimeoutError("timed out")):
            with self.assertRaises(gc.Unavailable) as caught:
                self.read(opener=self.opener(error=error))
            self.assertNotIn(self.TOKEN, str(caught.exception))
            self.assertNotIn(gc.READ_PATH, str(caught.exception))

    def test_junk_over_http_is_not_mistaken_for_an_answer(self):
        for body in ("not json", "[]", '{"error": "nope"}', "null"):
            with self.assertRaises(gc.Unavailable):
                self.read(opener=self.opener(body))

    def test_an_oversized_reply_is_refused_rather_than_half_read(self):
        # Something other than the console API answered. Truncating the JSON
        # must fail the read, never yield the campaigns it happened to contain.
        padded = json.dumps({"date": "2026-11-11", "campaigns": [row("a")],
                             "pad": "x" * (2 * gc.MAX_REPLY_BYTES)})
        with self.assertRaises(gc.Unavailable):
            self.read(opener=self.opener(padded))

    def test_without_a_token_it_uses_the_ssh_wrapper_and_opens_no_socket(self):
        # The operator path: deploy/gateway-campaigns.sh, which needs no secret.
        def refuse(*args, **kwargs):
            raise AssertionError("must not reach the console API")
        with unittest.mock.patch.object(gc.urllib.request, "urlopen", refuse):
            rows = gc.read(env={}, runner=lambda *a, **k: Completed(stdout=reply([row("a")])))
        self.assertEqual(rows[0]["slug"], "a")

    def test_a_blank_token_is_not_a_token(self):
        rows = gc.read(env={gc.GATE_TOKEN_VAR: "   "},
                       runner=lambda *a, **k: Completed(stdout=reply([row("a")])))
        self.assertEqual(rows[0]["slug"], "a")

    def test_the_workflow_hands_both_deploy_steps_the_token(self):
        workflow = (ROOT / ".github/workflows/deploy-main.yml").read_text(encoding="utf-8")
        self.assertEqual(workflow.count("CONSOLE_API_GATE_TOKEN: ${{ secrets.CONSOLE_API_GATE_TOKEN }}"), 2)
        self.assertIn(gc.GATE_TOKEN_VAR, workflow)


class ScopeTest(unittest.TestCase):
    """`railway ssh` resolves the project from a linked directory, and a CI
    checkout is never linked: the first release after the campaign data moved to
    Firestore failed this gate on nothing else, with the gateway healthy."""

    def test_the_wrapper_is_told_which_gateway_to_reach(self):
        env = gc.scoped({"PATH": "/bin"})
        self.assertEqual(env["RAILWAY_PROJECT"], gc.PROJECT)
        self.assertEqual(env["RAILWAY_SERVICE"], gc.SERVICE)
        self.assertEqual(env["RAILWAY_ENVIRONMENT"], gc.ENVIRONMENT)
        self.assertEqual(env["PATH"], "/bin")

    def test_a_scope_the_caller_chose_is_left_alone(self):
        env = gc.scoped({"RAILWAY_PROJECT": "other", "RAILWAY_SERVICE": ""})
        self.assertEqual(env["RAILWAY_PROJECT"], "other")
        # Empty means unset to deploy/gateway-exec.sh, which tests with -n.
        self.assertEqual(env["RAILWAY_SERVICE"], gc.SERVICE)

    def test_the_wrapper_reads_the_scope_from_the_environment(self):
        exec_sh = (ROOT / "deploy/gateway-exec.sh").read_text(encoding="utf-8")
        for name in gc.RAILWAY_SCOPE:
            self.assertIn(name, exec_sh)

    def test_a_run_carries_the_scope(self):
        seen = {}
        def runner(argv, **kwargs):
            seen.update(kwargs["env"])
            return Completed(stdout=reply([row("a")]))
        gc.read(runner=runner)
        self.assertEqual(seen["RAILWAY_SERVICE"], gc.SERVICE)

    def test_the_release_script_has_no_second_copy_of_the_scope(self):
        source = (ROOT / "deploy/railway-release.py").read_text(encoding="utf-8")
        self.assertIn("gateway_campaigns.SCOPE", source)
        for value in (gc.PROJECT, gc.SERVICE, gc.ENVIRONMENT):
            self.assertNotIn(value, source)


class WrapperTest(unittest.TestCase):
    def test_the_access_mechanism_is_sourced_not_repeated(self):
        # deploy/gateway-exec.sh exists so there is exactly one definition of
        # how to reach the gateway; a second `railway ssh` here would be a
        # third copy to keep in step.
        self.assertIn("gateway-exec.sh", WRAPPER)
        self.assertNotIn("railway ssh", WRAPPER)

    def test_it_runs_console_read_as_joseph(self):
        # Root trips the workspace plugins' own ownership check.
        self.assertIn("runuser -u joseph", WRAPPER)
        self.assertIn("console-read", WRAPPER)
        self.assertIn("campaigns", WRAPPER)

    def test_it_reads_and_changes_nothing(self):
        for word in ("cron add", "cron delete", "console-write", "--write", "rm "):
            self.assertNotIn(word, WRAPPER)

    def test_the_wrapper_is_executable(self):
        self.assertTrue((ROOT / "deploy/gateway-campaigns.sh").stat().st_mode & 0o111)


if __name__ == "__main__":
    unittest.main()
