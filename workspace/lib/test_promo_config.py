"""Which operator config a run acts on (workspace/lib/promo_config.py).

The claim worth testing is narrow and load-bearing: in firestore mode the
targets payload a run consumes is the SAME payload the committed targets.json
holds, for every campaign, so the cutover changes where the data comes from and
nothing else. The rest is the failure handling -- an unknown setting, and
Firestore having nothing trustworthy to give.

No emulator and no mock library: the fake Firestore from test_operator_config
answers over the same ``opener`` seam urllib uses, so this runs under
``--network none``.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes it.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

LIB = os.path.dirname(os.path.abspath(__file__))
if LIB not in sys.path:
    sys.path.insert(0, LIB)
ROOT = Path(LIB).parents[1]
if str(ROOT / "deploy") not in sys.path:
    sys.path.insert(0, str(ROOT / "deploy"))

import operator_config  # noqa: E402
import promo_config  # noqa: E402
import promo_membership  # noqa: E402
from test_operator_config import FakeFirestore, creds_b64  # noqa: E402

def _seed_module():
    """deploy/seed-firestore.py is not importable by name (it has a dash)."""
    import importlib.util

    path = ROOT / "deploy" / "seed-firestore.py"
    spec = importlib.util.spec_from_file_location("seed_firestore", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SEED = _seed_module()
CRON = str(ROOT / "workspace" / "cron")


def wire(docs):
    return dict((collection, dict((doc_id, operator_config.encode_fields(fields))
                                  for doc_id, fields in rows))
                for collection, rows in docs.items())


class ModeTest(unittest.TestCase):
    def test_unset_means_the_file(self):
        self.assertEqual(promo_config.mode({}), ("file", None))
        self.assertEqual(promo_config.mode({"GENAIE_CONFIG_SOURCE": ""}), ("file", None))

    def test_each_mode_is_recognised_however_it_is_typed(self):
        for raw, want in ((" file ", "file"), ("Shadow", "shadow"),
                          ("FIRESTORE", "firestore")):
            with self.subTest(raw=raw):
                self.assertEqual(promo_config.mode({"GENAIE_CONFIG_SOURCE": raw}),
                                 (want, None))

    def test_an_unknown_value_reads_as_the_file_and_is_reported(self):
        # A typo must not quietly change where ads go, in either direction.
        current, warning = promo_config.mode({"GENAIE_CONFIG_SOURCE": "firestoer"})
        self.assertEqual(current, "file")
        self.assertIn("is not one of", warning)

    def test_the_promo_key_is_the_job_name_without_the_suffix(self):
        self.assertEqual(promo_config.promo_key("genaie-leads-promo"), "genaie-leads")
        self.assertEqual(promo_config.promo_key("genaie-leads"), "genaie-leads")
        self.assertEqual(promo_config.promo_key(None), "")


class FileModeTest(unittest.TestCase):
    def setUp(self):
        self.membership = promo_membership.load(
            promo_membership.default_path(str(ROOT / "workspace")))
        self.assertIsNotNone(self.membership)

    def resolve(self, promo, targets_file=None, env=None):
        return promo_config.resolve(promo, targets_file=targets_file,
                                    membership_path=promo_membership.default_path(
                                        str(ROOT / "workspace")),
                                    env=env if env is not None else {},
                                    err=io.StringIO())

    def test_the_committed_targets_file_is_handed_back_unchanged(self):
        for promo in self.membership["promos"]:
            path = promo_membership.targets_path(self.membership, promo, CRON)
            with self.subTest(promo=promo):
                got = self.resolve(promo + "-promo", targets_file=path)
                with open(path, encoding="utf-8") as handle:
                    self.assertEqual(got["doc"], json.load(handle))
                self.assertEqual(got["source"], "file")

    def test_a_missing_targets_file_is_reported_and_leaves_an_empty_payload(self):
        err = io.StringIO()
        got = promo_config.resolve("genaie-promo", targets_file="/nope/targets.json",
                                   env={}, err=err)
        self.assertEqual(got["doc"], {})
        self.assertIn("targets file not found", err.getvalue())

    def test_the_manifest_supplies_the_switches(self):
        got = self.resolve("cv-promo")
        self.assertIs(got["enabled"], False)          # cv is off in promo-crons.json
        self.assertIs(got["switches"]["genaie"], True)

    def test_no_manifest_at_all_claims_nothing_is_switched_off(self):
        # Fail-open, matching promo_caps.disabled_promos: a capacity/pause nicety
        # must not be the reason a day's ads do not go out.
        got = self.resolve("cv-promo", env={"GENAIE_PROMO_CRONS": "/nope.json"})
        self.assertEqual(got["switches"], {})
        self.assertIsNone(got["enabled"])


class FirestoreModeTest(unittest.TestCase):
    def setUp(self):
        self.membership, self.switches = SEED.load_sources()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env = {"GENAIE_FIRESTORE_DIR": self.dir,
                    "GCP_SERVICE_ACCOUNT_B64": creds_b64(),
                    "GENAIE_CONFIG_SOURCE": "firestore"}
        self.firestore = FakeFirestore(wire(SEED.documents(self.membership, self.switches)),
                                       page_size=50)
        self.clock = [1_800_000_000.0]

    def resolve(self, promo, **kw):
        return promo_config.resolve(
            promo, env=self.env, err=io.StringIO(), opener=self.firestore,
            signer=lambda header, claims, key: "jwt", now=lambda: self.clock[0],
            sleep=lambda s: None, **kw)

    def test_every_campaign_resolves_to_its_committed_targets_file(self):
        # This is the cutover's whole promise: same payload, different store.
        for promo in self.membership["promos"]:
            path = promo_membership.targets_path(self.membership, promo, CRON)
            with self.subTest(promo=promo):
                got = self.resolve(promo + "-promo")
                self.assertEqual(got["source"], "firestore")
                with open(path, encoding="utf-8") as handle:
                    self.assertEqual(promo_membership.dumps_targets(got["doc"]),
                                     handle.read())

    def test_the_run_is_gated_by_the_store_alone(self):
        # ``enabled`` is the operator's live pause control, so it follows the
        # store even when the manifest disagrees -- cv is off in promo-crons.json.
        self.assertIs(self.resolve("cv-promo")["enabled"], False)
        self.firestore.store["campaigns"]["cv"]["enabled"] = {"booleanValue": True}
        self.assertIs(self.resolve("cv-promo")["enabled"], True)

    def test_the_switch_map_needs_both_switches_on(self):
        # The caps read this map, and must not hand a group's allowance to a
        # campaign that cannot post -- whichever switch is the reason.
        self.firestore.store["campaigns"]["cv"]["enabled"] = {"booleanValue": True}
        switches = self.resolve("genaie-promo")["switches"]
        self.assertIs(switches["cv"], False)       # no cron in promo-crons.json
        self.assertIs(switches["genaie"], True)

        self.firestore.store["campaigns"]["genaie"]["enabled"] = {"booleanValue": False}
        self.assertIs(self.resolve("genaie-promo")["switches"]["genaie"], False)

    def test_a_caller_that_names_no_campaign_still_gets_the_membership(self):
        got = self.resolve(None)
        self.assertEqual(got["doc"], {})
        self.assertEqual(len(got["membership"]["groups"]),
                         len(self.membership["groups"]))

    def test_an_outage_serves_the_snapshot_and_says_so(self):
        self.resolve("genaie-promo")           # leaves a snapshot behind
        self.firestore.down = True
        got = self.resolve("genaie-promo")
        self.assertEqual(got["source"], "snapshot")

    def test_an_outage_with_no_snapshot_raises_so_the_caller_sends_nothing(self):
        self.firestore.down = True
        with self.assertRaises(operator_config.ConfigUnavailable):
            self.resolve("genaie-promo")

    def test_the_repo_membership_is_never_the_fallback(self):
        # The file is sitting right there, readable, and would resolve fine in
        # file mode -- and must still not be used, because after the cutover it
        # is stale targeting: acting on it would re-advertise a group someone
        # had just paused.
        path = promo_membership.targets_path(self.membership, "genaie", CRON)
        from_file = promo_config.resolve("genaie-promo", targets_file=path, env={},
                                         err=io.StringIO())
        self.assertTrue(from_file["doc"]["groups"])

        self.firestore.down = True
        with self.assertRaises(operator_config.ConfigUnavailable):
            self.resolve("genaie-promo", targets_file=path)

    def test_a_quarantined_group_is_left_out_of_the_targets(self):
        jid = next(iter(self.membership["groups"]))
        self.firestore.store["groups"][jid]["status"] = {"stringValue": "banana"}
        got = self.resolve("genaie-promo")
        self.assertNotIn(jid, got["doc"].get("groups", []))
        self.assertIn(jid, got["report"]["quarantined"])


if __name__ == "__main__":
    unittest.main()
