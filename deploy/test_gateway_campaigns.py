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

    def test_a_nonzero_exit_reports_the_last_line_of_its_output(self):
        def runner(*args, **kwargs):
            return Completed(code=7, stderr="connecting...\nno such service\n")
        with self.assertRaises(gc.Unavailable) as caught:
            gc.read(runner=runner)
        self.assertIn("exit 7", str(caught.exception))
        self.assertIn("no such service", str(caught.exception))

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
