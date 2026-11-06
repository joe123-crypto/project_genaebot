"""What a campaign run does once Firestore decides where ads go.

promo_config is tested on its own (workspace/lib/test_promo_config.py); this
covers the three things only the engine can get wrong:

* a firestore-mode run sends to exactly the groups the file-mode run would,
* no trustworthy config means NO SEND and a non-zero exit, so the cron's failure
  alert fires rather than the day passing silently,
* a campaign the operator switched off sends nothing even though its cron is
  still registered on the gateway volume.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes it.
"""

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = str(ROOT / "workspace/lib")
for path in (LIB, str(ROOT / "deploy")):
    if path not in sys.path:
        sys.path.insert(0, path)

import operator_config  # noqa: E402
import promo_broadcast as pb  # noqa: E402
import promo_config  # noqa: E402
import promo_membership  # noqa: E402
from test_operator_config import FakeFirestore, creds_b64  # noqa: E402

CRON = str(ROOT / "workspace/cron")
DAY = date(2026, 11, 6)


def seed_module():
    spec = importlib.util.spec_from_file_location(
        "seed_firestore", ROOT / "deploy" / "seed-firestore.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SEED = seed_module()


def wire(docs):
    return dict((collection, dict((doc_id, operator_config.encode_fields(fields))
                                  for doc_id, fields in rows))
                for collection, rows in docs.items())


def pin(day):
    class _Date(date):
        @classmethod
        def today(cls):
            return day
    return _Date


class Harness(unittest.TestCase):
    """A real campaign run with a fake sender, a fake Firestore and no delays."""

    PROMO = "genaie"

    def setUp(self):
        self.membership, self.switches = SEED.load_sources()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.image = os.path.join(self.dir, "ad.png")
        with open(self.image, "wb") as handle:
            handle.write(b"img")
        self.state = os.path.join(self.dir, "state.json")
        self.targets = promo_membership.targets_path(self.membership, self.PROMO, CRON)
        self.env = {"GENAIE_FIRESTORE_DIR": os.path.join(self.dir, "fs"),
                    "GCP_SERVICE_ACCOUNT_B64": creds_b64(),
                    "GENAIE_CONFIG_SOURCE": "firestore"}
        self.firestore = FakeFirestore(wire(SEED.documents(self.membership, self.switches)),
                                       page_size=50)
        self.clock = [1_800_000_000.0]

        self.sent = []
        saved_send, saved_date = pb.send_ad, pb.date
        pb.send_ad = lambda target, text, image, account=None: (
            self.sent.append(target) or (True, '{"messageId":"m"}'))
        pb.date = pin(DAY)

        def restore():
            pb.send_ad, pb.date = saved_send, saved_date
        self.addCleanup(restore)

    def resolver(self, env=None):
        env = self.env if env is None else env

        def resolve(promo, targets_file=None, membership_path=None):
            return promo_config.resolve(
                promo, targets_file=targets_file, membership_path=membership_path,
                env=env, err=self.err, opener=self.firestore,
                signer=lambda header, claims, key: "jwt",
                now=lambda: self.clock[0], sleep=lambda s: None)
        return resolve

    def run_broadcast(self, env=None, argv=None, state=None):
        # One buffer for both: what the resolver reports and what the engine
        # reports both land on the run's stderr in production.
        self.err = io.StringIO()
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(self.err):
            rc = pb.broadcast(text="Ad {bot_number}", image=self.image,
                              targets_file=self.targets,
                              state_file=state or self.state,
                              promo_name=self.PROMO + "-promo",
                              send_delay=0, argv=argv or [], start_jitter=0,
                              caps=None, account_status=lambda a: {x: True for x in a},
                              resolve_config=self.resolver(env))
        return rc, out.getvalue(), self.err.getvalue()


class ParityTest(Harness):
    def test_firestore_sends_to_exactly_the_groups_the_file_would(self):
        rc, _, err = self.run_broadcast()
        self.assertEqual(rc, 0, err)
        from_firestore = list(self.sent)

        self.sent.clear()
        rc, _, err = self.run_broadcast(
            env={}, state=os.path.join(self.dir, "state-file.json"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(from_firestore, list(self.sent))
        self.assertTrue(from_firestore)


class FailClosedTest(Harness):
    def test_an_outage_with_no_snapshot_sends_nothing_and_exits_non_zero(self):
        self.firestore.down = True
        rc, out, err = self.run_broadcast()
        self.assertEqual(rc, 1)
        self.assertEqual(self.sent, [])
        self.assertEqual(out.strip(), "NO_REPLY")
        self.assertIn("no operator config to act on", err)

    def test_a_snapshot_inside_the_age_limit_still_sends(self):
        self.run_broadcast()                       # leaves a snapshot behind
        self.sent.clear()
        self.firestore.down = True
        self.clock[0] += operator_config.SNAPSHOT_MAX_AGE_SECONDS - 60
        rc, _, err = self.run_broadcast(state=os.path.join(self.dir, "state-2.json"))
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.sent)
        self.assertIn("snapshot", err)

    def test_a_snapshot_past_the_age_limit_sends_nothing(self):
        # Fail closed has to mean closed, not stale: a week-old snapshot would
        # keep advertising to a group the operator paused days ago.
        self.run_broadcast()
        self.sent.clear()
        self.firestore.down = True
        self.clock[0] += operator_config.SNAPSHOT_MAX_AGE_SECONDS + 60
        rc, out, err = self.run_broadcast(state=os.path.join(self.dir, "state-3.json"))
        self.assertEqual(rc, 1)
        self.assertEqual(self.sent, [])
        self.assertEqual(out.strip(), "NO_REPLY")
        self.assertIn("stale targeting", err)


class SwitchTest(Harness):
    def off(self):
        self.firestore.store["campaigns"][self.PROMO]["enabled"] = {"booleanValue": False}

    def test_a_campaign_switched_off_sends_nothing(self):
        # Its cron is still registered on the volume, so the run has to stand
        # down by itself -- that is what makes pausing a campaign a config edit
        # instead of an installer run.
        self.off()
        rc, out, err = self.run_broadcast()
        self.assertEqual(rc, 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(out.strip(), "NO_REPLY")
        self.assertIn("switched off", err)

    def test_a_switched_off_campaign_can_still_be_previewed(self):
        # Matching promo_caps, which never treats the promo being run as
        # disabled: an operator checking the copy of a paused campaign gets it.
        self.off()
        rc, out, err = self.run_broadcast(argv=["--dry-run"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.sent, [])
        self.assertIn("switched off", err)
        self.assertNotEqual(out.strip(), "NO_REPLY")

    def test_a_non_boolean_switch_counts_as_off(self):
        # Off is the safe reading of an ambiguous kill switch, and the Firebase
        # Console makes "true" as a string easy to type.
        self.firestore.store["campaigns"][self.PROMO]["enabled"] = {"stringValue": "true"}
        rc, out, _ = self.run_broadcast()
        self.assertEqual((rc, out.strip()), (0, "NO_REPLY"))
        self.assertEqual(self.sent, [])

    def test_the_file_mode_run_is_not_gated_by_the_manifest(self):
        # Before the cutover a switched-off campaign has no cron at all, and a
        # hand-run of one must keep working exactly as it does today.
        rc, _, err = self.run_broadcast(env={})
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.sent)


class SingleReadTest(Harness):
    def test_the_targets_are_resolved_once_per_run(self):
        reads = []
        resolve = self.resolver()

        def counting(promo, **kw):
            reads.append(promo)
            return resolve(promo, **kw)

        self.err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(self.err):
            pb.broadcast(text="Ad {bot_number}", image=self.image,
                         targets_file=self.targets, state_file=self.state,
                         promo_name=self.PROMO + "-promo", send_delay=0, argv=[],
                         start_jitter=0, caps=None,
                         account_status=lambda a: {x: True for x in a},
                         resolve_config=counting)
        self.assertEqual(reads, [self.PROMO + "-promo"])


if __name__ == "__main__":
    unittest.main()
