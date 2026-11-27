#!/usr/bin/env python3
"""Promoting a released commit to workspace bots.

What matters: nothing is promoted that is not on GHCR, the console is told the
exact image of the exact commit, a step that is switched off never fails the
gateway's release, and a refusal is reported in the console's words.
"""

import contextlib
import importlib.util
import io
import json
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("promote_workspace_bots", ROOT / "deploy/promote-workspace-bots.py")
promote = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(promote)
WORKFLOW = (ROOT / ".github/workflows/deploy-main.yml").read_text(encoding="utf-8")

SHA = "a" * 40
ENV = {"CONSOLE_URL": "https://console.example/", "FLEET_RELEASE_TOKEN": "f" * 40}


class Response(io.BytesIO):
    def __init__(self, body=b"{}", status=200):
        super().__init__(body)
        self.status = status


class Registry:
    """GHCR and the console, as far as this script sees them."""

    def __init__(self, published_after=0, console=None):
        self.asked = 0
        self.published_after = published_after
        self.console = console
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        url = request.full_url
        if "/token?" in url:
            return Response(json.dumps({"token": "anonymous"}).encode())
        if "/manifests/" in url:
            self.asked += 1
            if self.asked > self.published_after:
                return Response(status=200)
            raise urllib.error.HTTPError(url, 404, "not found", {}, io.BytesIO(b"{}"))
        if url.endswith("/api/internal/fleet/release"):
            if self.console:
                raise self.console
            return Response(json.dumps({"image": json.loads(request.data)["image"]}).encode())
        raise AssertionError("unexpected request %s" % url)


def run(registry, env=ENV, argv=("--sha", SHA, "--owner", "Joe123-Crypto")):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = promote.main(list(argv), env=env, opener=registry, sleep=lambda _: None,
                            clock=iter(range(0, 10_000, 30)).__next__)
    return code, out.getvalue() + err.getvalue()


class PromoteTest(unittest.TestCase):
    def test_tells_the_console_the_exact_image_once_it_is_published(self):
        registry = Registry(published_after=2)
        code, output = run(registry)
        self.assertEqual(code, 0, output)
        posted = [r for r in registry.requests if r.full_url.endswith("/fleet/release")]
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0].full_url, "https://console.example/api/internal/fleet/release")
        # Lower-cased, as GHCR names are and as publish-bot-image writes them.
        self.assertEqual(json.loads(posted[0].data), {"image": "ghcr.io/joe123-crypto/genaebot:" + SHA})
        self.assertEqual(posted[0].get_header("Authorization"), "Bearer " + "f" * 40)

    def test_promotes_nothing_that_never_appeared(self):
        registry = Registry(published_after=10_000)
        code, output = run(registry, argv=("--sha", SHA, "--owner", "x", "--wait-minutes", "2"))
        self.assertEqual(code, 1)
        self.assertIn("was not published", output)
        self.assertFalse([r for r in registry.requests if r.full_url.endswith("/fleet/release")])

    def test_switched_off_is_a_warning_not_a_failed_release(self):
        registry = Registry()
        code, output = run(registry, env={})
        self.assertEqual(code, 0)
        self.assertIn("::warning::", output)
        self.assertEqual(registry.requests, [])

    def test_a_refusal_is_reported_in_the_consoles_words_and_fails(self):
        refusal = urllib.error.HTTPError("https://console.example/api/internal/fleet/release", 400, "Bad Request",
                                         {}, io.BytesIO(b'{"error": "That image is not one of this console\'s."}'))
        code, output = run(Registry(console=refusal))
        self.assertEqual(code, 1)
        self.assertIn("not one of this console's", output)
        self.assertNotIn("f" * 40, output)

    def test_refuses_a_short_sha(self):
        self.assertEqual(run(Registry(), argv=("--sha", "abc123", "--owner", "x"))[0], 2)

    def test_the_workflow_promotes_only_after_the_gateway_released(self):
        job = WORKFLOW[WORKFLOW.index("\n  promote-workspace-bots:\n"):]
        self.assertIn("needs: [deploy]", job)
        self.assertIn("github.ref == 'refs/heads/main' && github.event_name != 'pull_request'", job)
        self.assertIn("FLEET_RELEASE_TOKEN: ${{ secrets.FLEET_RELEASE_TOKEN }}", job)
        self.assertIn("CONSOLE_URL: ${{ vars.CONSOLE_URL }}", job)
        self.assertIn('python3 deploy/promote-workspace-bots.py --sha "$GITHUB_SHA"', job)


if __name__ == "__main__":
    unittest.main()
