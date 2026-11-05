"""The parity gate for moving operator config into Firestore.

Runs the repository's real membership.json and promo-crons.json through the
seed (deploy/seed-firestore.py), Firestore's wire encoding, and back out through
operator_config -- the path production will read -- and requires the result to be
identical: every field, the order of groups, lines and campaigns, and the
targets.json generated for each campaign, byte for byte.

Offline by default: Firestore is the in-memory fake from
workspace/lib/test_operator_config.py, which lists documents by ID the way the
real service does, so a lost order would show here. With
GENAIE_FIRESTORE_INTEGRATION=1 and GCP_SERVICE_ACCOUNT_FILE set, it also runs
``seed-firestore.py --verify`` against the real project.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes them.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "workspace" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import operator_config  # noqa: E402
import promo_membership  # noqa: E402
from test_operator_config import FakeFirestore, creds_b64  # noqa: E402

SEED_PATH = ROOT / "deploy" / "seed-firestore.py"
_spec = importlib.util.spec_from_file_location("seed_firestore", SEED_PATH)
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)


def wire(docs):
    """Seed documents as Firestore would hold them: encoded, keyed by ID."""
    return dict((collection, dict((doc_id, operator_config.encode_fields(fields))
                                  for doc_id, fields in rows))
                for collection, rows in docs.items())


class ParityTest(unittest.TestCase):
    def setUp(self):
        self.membership, self.switches = seed.load_sources()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env = {"GENAIE_FIRESTORE_DIR": self.dir, "GCP_SERVICE_ACCOUNT_B64": creds_b64()}

    def read_back(self, docs):
        firestore = FakeFirestore(wire(docs), page_size=37)  # forces several pages
        return operator_config.load(self.env, opener=firestore,
                                    signer=lambda h, c, k: "jwt", now=lambda: 1.8e9,
                                    sleep=lambda s: None, log=lambda m: None)

    def test_the_real_membership_survives_the_trip_exactly(self):
        result = self.read_back(seed.documents(self.membership, self.switches))
        self.assertEqual(result["source"], "firestore")
        self.assertEqual(result["report"]["quarantined"], {})
        self.assertEqual(result["report"]["warnings"], [])
        self.assertEqual(seed.parity_diff(self.membership, self.switches,
                                          result["membership"], result["enabled"]), [])

    def test_every_campaigns_targets_file_is_byte_identical(self):
        # The payload the send path consumes, compared the way CI already
        # compares the committed targets.json files.
        got = self.read_back(seed.documents(self.membership, self.switches))["membership"]
        for promo in self.membership["promos"]:
            self.assertEqual(
                promo_membership.dumps_targets(promo_membership.targets_payload(got, promo)),
                promo_membership.dumps_targets(promo_membership.targets_payload(self.membership, promo)),
                promo)

    def test_the_switches_match_the_cron_manifest(self):
        result = self.read_back(seed.documents(self.membership, self.switches))
        self.assertEqual(result["enabled"], self.switches)

    def test_every_group_line_and_campaign_becomes_one_document(self):
        docs = seed.documents(self.membership, self.switches)
        self.assertEqual(len(docs["groups"]), len(self.membership["groups"]))
        self.assertEqual(len(docs["lines"]), len(self.membership["defaults"]["accounts"]))
        self.assertEqual(len(docs["campaigns"]), len(self.membership["promos"]))
        self.assertEqual([doc_id for doc_id, _ in docs["settings"]], ["defaults"])


class ParityDiffTest(unittest.TestCase):
    """The gate is only worth having if it fails when something is wrong."""

    def setUp(self):
        self.membership, self.switches = seed.load_sources()

    def copy(self):
        return json.loads(json.dumps(self.membership))

    def test_a_changed_field_is_named(self):
        got = self.copy()
        jid = next(iter(got["groups"]))
        got["groups"][jid]["status"] = "on-hold"
        problems = seed.parity_diff(self.membership, self.switches, got, self.switches)
        self.assertEqual(problems, ["groups/%s differs in status" % jid])

    def test_a_lost_order_is_caught_even_when_every_field_matches(self):
        got = self.copy()
        got["groups"] = dict(reversed(list(got["groups"].items())))
        self.assertIn("group order differs",
                      seed.parity_diff(self.membership, self.switches, got, self.switches))

    def test_a_missing_document_is_caught(self):
        got = self.copy()
        del got["groups"][next(iter(got["groups"]))]
        problems = seed.parity_diff(self.membership, self.switches, got, self.switches)
        self.assertTrue(any("missing from Firestore" in p for p in problems))

    def test_a_flipped_switch_is_caught(self):
        flipped = dict(self.switches, cv=not self.switches["cv"])
        problems = seed.parity_diff(self.membership, self.switches, self.membership, flipped)
        self.assertEqual(problems, ["campaigns/cv differs in enabled"])


class SeedPlanTest(unittest.TestCase):
    def test_a_second_seed_writes_nothing(self):
        membership, switches = seed.load_sources()
        docs = seed.documents(membership, switches)
        writes, strays, unchanged = seed.plan_writes(docs, docs)
        self.assertEqual((writes, strays), ([], []))
        self.assertEqual(unchanged, sum(len(rows) for rows in docs.values()))

    def test_documents_only_in_firestore_are_reported_not_silently_kept(self):
        membership, switches = seed.load_sources()
        docs = seed.documents(membership, switches)
        existing = dict(docs, groups=docs["groups"] + [("stray@g.us", {"name": "x"})])
        _, strays, _ = seed.plan_writes(docs, existing)
        self.assertEqual(strays, [("groups", "stray@g.us")])

    def test_a_manifest_and_membership_that_disagree_are_refused(self):
        membership, switches = seed.load_sources()
        with self.assertRaises(seed.SeedError):
            seed.documents(membership, dict(switches, ghost=True))


@unittest.skipUnless(os.environ.get("GENAIE_FIRESTORE_INTEGRATION") == "1",
                     "set GENAIE_FIRESTORE_INTEGRATION=1 and GCP_SERVICE_ACCOUNT_FILE "
                     "to verify against the real project")
class LiveParityTest(unittest.TestCase):
    def test_the_real_project_matches_the_file(self):
        run = subprocess.run([sys.executable, str(SEED_PATH), "--verify"],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("parity: exact", run.stdout)


if __name__ == "__main__":
    unittest.main()
