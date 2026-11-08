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


def wire_unordered(docs):
    """As ``wire``, but with every document's fields in alphabetical order --
    which is how real Firestore hands them back. Exporting from this proves the
    file's own key order is kept on purpose, not by the fake's insertion order."""
    def sort_fields(encoded):
        return dict(sorted(encoded.items()))
    return dict((collection, dict((doc_id, sort_fields(operator_config.encode_fields(fields)))
                                  for doc_id, fields in rows))
                for collection, rows in docs.items())


class ExportTest(unittest.TestCase):
    """seed-firestore.py --export: Firestore back into membership.json, so the file
    stays a truthful rollback once the console is editing the store."""

    def setUp(self):
        from unittest import mock
        self.membership, self.switches = seed.load_sources()
        self.real = seed.MEMBERSHIP.read_text(encoding="utf-8")
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        # Never the repo's own file: every export here writes a temp copy.
        self.target = self.dir / "membership.json"
        self.target.write_text(self.real, encoding="utf-8")
        patcher = mock.patch.object(seed, "MEMBERSHIP", self.target)
        patcher.start()
        self.addCleanup(patcher.stop)
        key = self.dir / "key.json"
        import base64
        key.write_text(base64.b64decode(creds_b64()).decode(), encoding="utf-8")
        self.key = str(key)
        self.store = wire_unordered(seed.documents(self.membership, self.switches))

    def export(self):
        import contextlib
        import io
        out = io.StringIO()
        firestore = FakeFirestore(self.store, page_size=37)
        with contextlib.redirect_stdout(out):
            code = seed.export(self.key, opener=firestore,
                               signer=lambda header, claims, key: "jwt",
                               now=lambda: 1_800_000_000.0, sleep=lambda s: None)
        return code, out.getvalue()

    def test_an_unchanged_store_reproduces_the_committed_file_byte_for_byte(self):
        code, out = self.export()
        self.assertEqual(code, 0, out)
        self.assertIn("already matches", out)
        self.assertEqual(self.target.read_text(encoding="utf-8"), self.real)

    def test_a_store_edit_changes_only_its_own_lines(self):
        import difflib
        jid = next(j for j, e in self.membership["groups"].items()
                   if promo_membership.is_active(e) and e.get("promos"))
        promo = self.membership["groups"][jid]["promos"][0]
        self.store["groups"][jid]["paused_promos"] = operator_config.encode_value([promo])
        code, out = self.export()
        self.assertEqual(code, 0, out)
        written = self.target.read_text(encoding="utf-8")
        changed = [line for line in difflib.unified_diff(
            self.real.splitlines(), written.splitlines(), lineterm="", n=0)
            if line[:1] in "+-" and line[:3] not in ("+++", "---")]
        self.assertTrue(changed)
        self.assertTrue(all("paused_promos" in line or line.strip(" +-\",[]") in
                            ("", promo) for line in changed), changed)
        # And the new file is an exact mirror of the store.
        firestore = FakeFirestore(self.store, page_size=37)
        _, raw = operator_config.fetch(
            {"GCP_SERVICE_ACCOUNT_B64": creds_b64(),
             "GENAIE_FIRESTORE_DIR": str(self.dir / "fs")},
            opener=firestore, signer=lambda h, c, k: "jwt",
            now=lambda: 1_800_000_000.0, sleep=lambda s: None)
        got, got_switches, _ = operator_config.assemble(raw)
        self.assertEqual(operator_config.parity_diff(
            json.loads(written), self.switches, got, got_switches), [])

    def test_a_store_that_would_quarantine_a_group_is_not_exported(self):
        jid = next(iter(self.membership["groups"]))
        self.store["groups"][jid]["status"] = operator_config.encode_value("banana")
        code, out = self.export()
        self.assertEqual(code, 1)
        self.assertIn("CANNOT EXPORT", out)
        self.assertEqual(self.target.read_text(encoding="utf-8"), self.real)

    def test_a_switch_that_a_rollback_would_undo_is_named(self):
        self.store["campaigns"]["genaie"]["enabled"] = operator_config.encode_value(False)
        code, out = self.export()
        self.assertEqual(code, 0, out)
        self.assertIn("NOTE  genaie is OFF in Firestore but ON in promo-crons.json", out)

    def test_a_missing_file_is_recreated_from_the_store(self):
        # After phase 5 deletes the file, an export is how it comes back.
        self.target.unlink()
        code, out = self.export()
        self.assertEqual(code, 0, out)
        self.assertIn("(created)", out)
        recreated = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(recreated, json.loads(self.real))
        self.assertEqual(promo_membership.validate(recreated), [])


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
