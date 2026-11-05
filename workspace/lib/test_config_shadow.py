"""Shadow mode (phase 2): Firestore is read beside membership.json and every
difference is recorded, while the run acts on the file no matter what.

The send-path tests that matter most here are the negative ones: a missing key,
a dead network, a hung Firestore or a bug in the shadow code must change
nothing about the run it rides along with.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes them.
"""

import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

LIB = Path(__file__).resolve().parent
ROOT = LIB.parent.parent
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import config_shadow  # noqa: E402
import operator_config  # noqa: E402
from test_operator_config import FakeFirestore, creds_b64  # noqa: E402

_spec = importlib.util.spec_from_file_location("seed_firestore",
                                               ROOT / "deploy" / "seed-firestore.py")
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)

INJECT = dict(signer=lambda h, c, k: "jwt", now=lambda: 1.8e9, sleep=lambda s: None)


def wire(docs):
    return dict((collection, dict((doc_id, operator_config.encode_fields(fields))
                                  for doc_id, fields in rows))
                for collection, rows in docs.items())


class ModeTest(unittest.TestCase):
    def test_off_unless_asked(self):
        self.assertEqual(config_shadow.mode({}), ("file", None))
        self.assertEqual(config_shadow.mode({"GENAIE_CONFIG_SOURCE": "file"}), ("file", None))
        self.assertEqual(config_shadow.mode({"GENAIE_CONFIG_SOURCE": " Shadow "}), ("shadow", None))

    def test_the_cutover_value_is_not_honoured_early(self):
        # Setting phase 3's value before the cutover code exists must not look
        # like it worked: the run stays on the file and says so.
        current, warning = config_shadow.mode({"GENAIE_CONFIG_SOURCE": "firestore"})
        self.assertEqual(current, "file")
        self.assertIn("not supported yet", warning)


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.membership, self.switches = seed.load_sources()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env = {"GENAIE_FIRESTORE_DIR": self.dir, "GCP_SERVICE_ACCOUNT_B64": creds_b64(),
                    "GENAIE_CONFIG_SOURCE": "shadow"}
        self.firestore = FakeFirestore(wire(seed.documents(self.membership, self.switches)),
                                       page_size=50)

    def check(self, **overrides):
        inject = dict(INJECT, opener=self.firestore)
        inject.update(overrides)
        return config_shadow.check(self.env, **inject)

    def test_a_faithful_copy_is_exact_and_recorded_privately(self):
        result = self.check()
        self.assertEqual((result["status"], result["source"], result["problems"]),
                         ("exact", "firestore", []))
        path = config_shadow.result_path(self.env)
        self.assertEqual(config_shadow.read_result(self.env)["status"], "exact")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_an_edit_in_the_console_is_named(self):
        jid = next(iter(self.membership["groups"]))
        self.firestore.store["groups"][jid]["status"] = {"stringValue": "on-hold"}
        result = self.check()
        self.assertEqual(result["status"], "drift")
        self.assertEqual(result["problems"], ["groups/%s differs in status" % jid])

    def test_a_switched_off_campaign_is_drift(self):
        self.firestore.store["campaigns"]["cv"]["enabled"] = {"booleanValue": not self.switches["cv"]}
        self.assertEqual(self.check()["problems"], ["campaigns/cv differs in enabled"])

    def test_a_quarantined_group_says_why_not_just_that_it_is_missing(self):
        jid = next(iter(self.membership["groups"]))
        self.firestore.store["groups"][jid]["status"] = {"stringValue": "Active"}
        problems = self.check()["problems"]
        self.assertTrue(problems[0].startswith("quarantined %s:" % jid), problems)
        self.assertIn("groups/%s is missing from Firestore" % jid, problems)

    def test_unreachable_with_no_snapshot_is_unavailable_not_an_exception(self):
        self.firestore.down = True
        result = self.check()
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("no snapshot", result["degraded"])

    def test_unreachable_after_a_good_read_compares_the_snapshot_and_says_so(self):
        self.check()
        self.firestore.down = True
        result = self.check()
        self.assertEqual((result["status"], result["source"]), ("exact", "snapshot"))
        self.assertTrue(any("compared with the snapshot" in line
                            for line in config_shadow.summary(result)))

    def test_a_bug_inside_the_check_is_contained(self):
        def broken(request, timeout=None):
            raise RuntimeError("boom")
        result = self.check(opener=broken)
        self.assertEqual(result["status"], "error")
        self.assertIn("RuntimeError: boom", result["problems"][0])

    def test_a_missing_membership_file_is_an_error_result(self):
        result = config_shadow.check(self.env, os.path.join(self.dir, "nope.json"),
                                     opener=self.firestore, **INJECT)
        self.assertEqual(result["status"], "error")
        self.assertIn("file side unreadable", result["problems"][0])


class AlongsideTest(unittest.TestCase):
    """The wrapper around every campaign run."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_off_means_no_thread_and_no_output(self):
        err = io.StringIO()
        self.assertIsNone(config_shadow.start({}, err=err))
        self.assertIsNone(config_shadow.finish(None, err=err))
        self.assertEqual(err.getvalue(), "")

    def test_the_runs_result_and_exceptions_pass_through_untouched(self):
        env = {"GENAIE_CONFIG_SOURCE": "shadow", "GENAIE_FIRESTORE_DIR": self.dir}
        calls = []

        @config_shadow.alongside
        def run(**kwargs):
            calls.append(kwargs)
            if kwargs.get("explode"):
                raise ValueError("the run's own error")
            return 7

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(sys, "stdout", stdout), mock.patch.object(sys, "stderr", stderr):
            os.environ.pop("GCP_SERVICE_ACCOUNT_B64", None)
            os.environ.pop("GCP_SERVICE_ACCOUNT_FILE", None)
            self.assertEqual(run(membership_path=None), 7)
            with self.assertRaisesRegex(ValueError, "the run's own error"):
                run(explode=True)
        self.assertEqual(len(calls), 2)
        # stdout is a cron script's reply; shadow mode must never write to it.
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("[config-shadow]", stderr.getvalue())

    def test_a_hung_firestore_is_abandoned_not_waited_for(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def hang(request, timeout=None):
            release.wait(30)
            raise RuntimeError("released")

        env = {"GENAIE_CONFIG_SOURCE": "shadow", "GENAIE_FIRESTORE_DIR": self.dir,
               "GCP_SERVICE_ACCOUNT_B64": creds_b64()}
        err = io.StringIO()
        handle = config_shadow.start(env, err=err, opener=hang, **INJECT)
        self.assertIsNone(config_shadow.finish(handle, timeout=0.2, err=err))
        self.assertIn("still running after 0 s", err.getvalue())
        self.assertIn("used membership.json", err.getvalue())

    def test_shadow_mode_wraps_the_real_broadcast(self):
        import promo_broadcast
        self.assertTrue(hasattr(promo_broadcast.broadcast, "__wrapped__"))
        inner = promo_broadcast.broadcast.__wrapped__
        self.assertEqual(inner.__wrapped__.__name__, "broadcast")


if __name__ == "__main__":
    unittest.main()
