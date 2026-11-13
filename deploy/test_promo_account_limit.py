"""The shared promo engine stops using a WhatsApp line that WhatsApp is limiting.

A rate limit (``rate-overlimit`` / 429) or an account restriction (Baileys v7
error 463, "Your account is restricted") is about the sending account, not one
group. Before this, the engine treated the first as an ordinary hard failure and
walked on through every remaining group, and the second matched the
admins-only-group marker ``restricted`` and was skipped as harmless. Either way
a limited line kept posting to a hundred groups, which is what turns a
temporary restriction into a ban. These tests pin the halt: one attempt, the
rest of that account's groups deferred, nothing marked sent, exit non-zero;
other accounts keep their turn.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes them.
"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for _path in (str(ROOT / "workspace/lib"), str(ROOT / "deploy")):
    if _path not in sys.path:
        sys.path.insert(0, _path)
import promo_broadcast as pb  # noqa: E402
import json_store  # noqa: E402
import promo_fixture  # noqa: E402

OK = (True, '{"messageId":"m"}')
LIMITED = (
    "OutboundDeliveryError: rate-overlimit <- Error: rate-overlimit",
    "OutboundDeliveryError: Your account is restricted <- Error: 463",
    "send failed (status 429)",
    "reachout timelock active",
    # The restriction wording is not pinned down, so the pair "account" + the
    # verb has to carry it, not one phrasing.
    "OutboundDeliveryError: your account has been restricted",
    "Boom [Error]: Connection Failure, data: { statusCode: 463 }",
)


class AccountLimitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.image = self.tmp / "ad.png"
        self.image.write_bytes(b"img")
        self.sent = []
        self._saved_send = pb.send_ad
        pb.send_ad = self._fake_send
        self.result = lambda jid: OK
        self.stderr = ""

    def tearDown(self):
        pb.send_ad = self._saved_send
        self._tmp.cleanup()

    def _fake_send(self, target, text, image, account=None):
        self.sent.append(target)
        return self.result(target)

    def _run(self, groups, state, **kwargs):
        kwargs.setdefault("resolve_config", promo_fixture.resolver_for(groups))
        kwargs.setdefault("account_status", lambda accounts: {a: True for a in accounts})
        kwargs.setdefault("cron_timeout", None)
        kwargs.setdefault("start_jitter", 0)
        kwargs.setdefault("caps", None)
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = pb.broadcast(text="hi", image=str(self.image), state_file=str(state),
                              promo_name="alpha-promo", argv=[], sleep=lambda s: None,
                              **kwargs)
        self.stderr = err.getvalue()
        return rc

    def test_limited_line_is_halted_after_one_attempt(self):
        groups = ["%d@g.us" % i for i in range(1, 6)]
        for out in LIMITED:
            with self.subTest(out=out):
                self.sent.clear()
                self.result = lambda jid, out=out: (False, out)
                state = self.tmp / ("state-%d.json" % LIMITED.index(out))
                self.assertEqual(self._run(groups, state), 1)
                self.assertEqual(self.sent, ["1@g.us"])
                self.assertEqual(json_store.load_json(str(state), {}).get("sent", {}), {})
                self.assertIn("HALTED account", self.stderr)
                self.assertNotIn("SKIPPED (no permission", self.stderr)

    def test_limit_mid_run_keeps_earlier_sends_and_defers_the_rest(self):
        groups = ["1@g.us", "2@g.us", "3@g.us", "4@g.us"]
        self.result = lambda jid: (False, LIMITED[0]) if jid == "2@g.us" else OK
        state = self.tmp / "state.json"
        self.assertEqual(self._run(groups, state), 1)
        self.assertEqual(self.sent, ["1@g.us", "2@g.us"])
        self.assertEqual(set(json_store.load_json(str(state), {})["sent"]), {"1@g.us"})
        self.assertIn("DEFERRED 2 targets", self.stderr)

    def test_admins_only_group_is_still_a_quiet_skip(self):
        self.result = lambda jid: ((False, "OutboundDeliveryError: forbidden")
                                   if jid == "1@g.us" else OK)
        state = self.tmp / "state.json"
        self.assertEqual(self._run(["1@g.us", "2@g.us"], state), 0)
        self.assertEqual(self.sent, ["1@g.us", "2@g.us"])

    def test_classifiers(self):
        self.assertTrue(pb.is_permission_error("forbidden: only admins can send"))
        self.assertTrue(pb.is_permission_error("group is restricted to admins"))
        self.assertFalse(pb.is_permission_error("Your account is restricted"))
        for out in LIMITED:
            self.assertTrue(pb.is_account_limited(out), out)
        self.assertFalse(pb.is_permission_error("this account is temporarily restricted"))
        self.assertTrue(pb.is_account_limited("this account is temporarily restricted"))
        # A status code counts only where the line presents it AS a status.
        # ``out`` is the whole CLI stdout+stderr, so a byte count, a duration or
        # a log timestamp must never halt the line.
        for out in ("Sent message 3EB0463A -> sha256:be883eeb1d13 (463ms)",
                    "OutboundDeliveryError: send failed <- timeout after 463 ms",
                    "gateway transport error: Command timed out, 429 bytes written",
                    "2026-11-13 09:04:12,463 WARN send failed",
                    "120363429000111@g.us", "took 1.429s", "forbidden",
                    "No active WhatsApp Web listener"):
            self.assertFalse(pb.is_account_limited(out), out)
        # An admins-only refusal names the sending line too ("(account: ads)").
        # That must not pair with the GROUP's "restricted" and halt the account.
        admins_only = ("OutboundDeliveryError: forbidden (account: ads) "
                       "120363407177302513@g.us is restricted to admins")
        self.assertFalse(pb.is_account_limited(admins_only))
        self.assertTrue(pb.is_permission_error(admins_only))


if __name__ == "__main__":
    unittest.main()
