"""What a campaign run acts on, now that Firestore is the only source.

promo_config used to choose between the committed membership and targets files
and the store, so most of this file was a parity argument: the two sources had
to agree, and the file had to stay usable so flipping back was a true rollback.
The files are gone, so what is left to pin is the behaviour that matters without
them -- it fails closed, the snapshot is the only fallback, a campaign is on only
if both its switches are, and a group the reader cannot decode is left out
rather than guessed at.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes it.
"""

import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "workspace" / "lib") not in sys.path:
    sys.path.insert(0, str(ROOT / "workspace" / "lib"))
if str(ROOT / "deploy") not in sys.path:
    sys.path.insert(0, str(ROOT / "deploy"))

import operator_config  # noqa: E402
import promo_config  # noqa: E402
import promo_membership  # noqa: E402
import promo_fixture  # noqa: E402
from test_operator_config import FakeFirestore, creds_b64  # noqa: E402


class RetiredModeTest(unittest.TestCase):
    def test_the_variable_unset_or_firestore_says_nothing(self):
        self.assertIsNone(promo_config.retired_mode({}))
        self.assertIsNone(promo_config.retired_mode({"GENAIE_CONFIG_SOURCE": ""}))
        for raw in ("firestore", " FIRESTORE "):
            self.assertIsNone(promo_config.retired_mode({"GENAIE_CONFIG_SOURCE": raw}))

    def test_asking_for_a_source_that_is_gone_is_reported(self):
        # An operator flipping this back to "file" expecting a rollback has to
        # be told there is nothing to roll back to, not watch the campaign send
        # from the store anyway in silence.
        for raw in ("file", "shadow", "firestoer"):
            warning = promo_config.retired_mode({"GENAIE_CONFIG_SOURCE": raw})
            self.assertIsNotNone(warning, raw)
            self.assertIn("no longer selects anything", warning)
            self.assertIn("Unset it", warning)

    def test_the_promo_key_is_the_job_name_without_the_suffix(self):
        self.assertEqual(promo_config.promo_key("genaie-leads-promo"), "genaie-leads")
        self.assertEqual(promo_config.promo_key("genaie-leads"), "genaie-leads")
        self.assertEqual(promo_config.promo_key(None), "")


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.membership = promo_fixture.membership()
        self.switches = promo_fixture.switches(self.membership)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env = {"GENAIE_FIRESTORE_DIR": self.dir,
                    "GCP_SERVICE_ACCOUNT_B64": creds_b64(),
                    "GENAIE_CONFIG_SOURCE": "firestore"}
        self.firestore = FakeFirestore(
            promo_fixture.wire(promo_fixture.documents(self.membership, self.switches)),
            page_size=50)
        self.clock = [1_800_000_000.0]

    def resolve(self, promo, **kw):
        return promo_config.resolve(
            promo, env=self.env, err=io.StringIO(), opener=self.firestore,
            signer=lambda header, claims, key: "jwt", now=lambda: self.clock[0],
            sleep=lambda s: None, **kw)

    def test_every_campaign_resolves_to_the_groups_configured_for_it(self):
        for promo in self.membership["promos"]:
            with self.subTest(promo=promo):
                got = self.resolve(promo + "-promo")
                self.assertEqual(got["source"], "firestore")
                self.assertEqual(got["mode"], "firestore")
                daily, _weekly = promo_membership.groups_for_promo(self.membership, promo)
                self.assertEqual(got["doc"].get("groups", []), daily)

    def test_the_run_is_gated_by_the_store_switch_alone(self):
        # ``enabled`` is the operator's live pause control, and it is what the
        # run itself stands down on.
        self.assertIs(self.resolve("paused-promo")["enabled"], False)
        self.firestore.store["campaigns"]["paused"]["enabled"] = {"booleanValue": True}
        self.assertIs(self.resolve("paused-promo")["enabled"], True)

    def test_the_switch_map_needs_both_switches_on(self):
        # The caps read this map and must not hand a group's allowance to a
        # campaign that cannot post -- whichever switch is the reason.
        got = self.resolve("alpha-promo")
        self.assertIs(got["switches"]["alpha"], True)
        self.assertIs(got["switches"]["paused"], False)       # store switch off

        # The other switch: a schedule the reconciler has not verified yet.
        self.firestore.store["campaigns"]["beta"]["scheduleStatus"] = {"stringValue": "pending"}
        got = self.resolve("alpha-promo")
        self.assertIs(got["switches"]["beta"], False)
        self.assertIs(got["cronSwitches"]["beta"], False)
        self.assertIs(got["storeSwitches"]["beta"], True)     # still on in the store

    def test_a_caller_that_names_no_campaign_still_gets_the_membership(self):
        got = self.resolve(None)
        self.assertEqual(got["doc"], {})
        self.assertEqual(len(got["membership"]["groups"]),
                         len(self.membership["groups"]))

    def test_an_outage_serves_the_snapshot_and_says_so(self):
        self.resolve("alpha-promo")           # leaves a snapshot behind
        self.firestore.down = True
        got = self.resolve("alpha-promo")
        self.assertEqual(got["source"], "snapshot")

    def test_an_outage_with_no_snapshot_raises_so_the_caller_sends_nothing(self):
        self.firestore.down = True
        with self.assertRaises(operator_config.ConfigUnavailable):
            self.resolve("alpha-promo")

    def test_there_is_no_second_source_to_fall_back_to(self):
        # The file that used to sit here, readable, is gone -- and even while it
        # existed it was refused, because by then it was stale targeting:
        # acting on it would re-advertise a group someone had just paused.
        self.assertFalse((ROOT / "workspace" / "cron").exists())
        self.firestore.down = True
        with self.assertRaises(operator_config.ConfigUnavailable):
            self.resolve("alpha-promo")

    def test_a_quarantined_group_is_left_out_of_the_targets(self):
        jid = next(iter(self.membership["groups"]))
        self.firestore.store["groups"][jid]["status"] = {"stringValue": "banana"}
        got = self.resolve("alpha-promo")
        self.assertNotIn(jid, got["doc"].get("groups", []))
        self.assertIn(jid, got["report"]["quarantined"])


if __name__ == "__main__":
    unittest.main()
