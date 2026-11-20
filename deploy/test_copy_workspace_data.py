"""Copying an install's documents into its workspace, and back.

This is the migration: the live root collections are copied into
``workspaces/<id>/`` before the bot is pointed there. The cases are the ones
that decide whether it is safe to run against production: a dry run writes
nothing, a write is exact down to Firestore's value types, verify catches every
kind of difference, nothing unknown is left behind, the console's registry is
never touched, and the source is only deleted when nothing would be lost.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes it. Fully offline.
"""

import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT / "workspace/lib"), str(ROOT / "deploy")):
    if path not in sys.path:
        sys.path.insert(0, path)

from firestore_tree_fake import FakeTree  # noqa: E402

PROJECT = "genaie-test"
WID = "wk-alpha1"


def load_script(name, filename):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / "deploy" / filename))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


TOOL = load_script("copy_workspace_data", "copy-workspace-data.py")


def production_like():
    """One document per collection, using every value type the store holds."""
    return {
        "settings/defaults": {"version": {"integerValue": "1"}, "caps_enabled": {"booleanValue": True},
                              "bands": {"mapValue": {"fields": {"normal": {"arrayValue": {"values": [
                                  {"integerValue": "1"}, {"integerValue": "3"}]}}}}}},
        "lines/default": {"number": {"stringValue": "+26370000001"}, "rank": {"integerValue": "10"}},
        "campaigns/web": {"kind": {"stringValue": "console"}, "enabled": {"booleanValue": True},
                          "ratio": {"doubleValue": 0.5}, "comment": {"nullValue": None}},
        "groups/1@g.us": {"status": {"stringValue": "active"},
                          "promos": {"arrayValue": {"values": [{"stringValue": "web"}]}}},
        "assets/web-1": {"campaign": {"stringValue": "web"}},
        "audit/20261120T000000Z-ab": {"at": {"timestampValue": "2026-11-20T00:00:00Z"}},
        "campaign_creations/req-1": {"slug": {"stringValue": "web"}},
        "campaign_deletions/old": {"name": {"stringValue": "Old"}},
        "campaign_asset_uploads/r1": {"expiresAt": {"integerValue": "99"}},
        "schedule_operations/op-1": {"digest": {"stringValue": "d"}},
        # The console's registry sits beside them and must never move.
        "consoleUsers/uid-1": {"workspaceId": {"stringValue": WID}},
        "workspaces/%s" % WID: {"ownerUid": {"stringValue": "uid-1"}},
        # So does another app's data: agent_genaie shares the project.
        "users/uid-1": {"plan": {"stringValue": "pro"}},
        "pendingGmailTokens/abc": {"ciphertext": {"stringValue": "opaque"}},
    }


class CopyTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        key = os.path.join(self.dir, "key.json")
        Path(key).write_text(json.dumps({"project_id": PROJECT, "client_email": "seed@x",
                                         "private_key": "k"}))
        self.env = {"GCP_SERVICE_ACCOUNT_FILE": key}
        self.firestore = FakeTree(PROJECT, production_like())

    def run_tool(self, *argv):
        out = io.StringIO()
        code = TOOL.main(list(argv), self.env, opener=self.firestore, signer=lambda *a: "jwt",
                         now=lambda: 1_800_000_000.0, sleep=lambda s: None, out=out)
        return code, out.getvalue()

    def workspace_docs(self):
        prefix = "workspaces/%s/" % WID
        return dict((path[len(prefix):], fields) for path, fields in self.firestore.docs.items()
                    if path.startswith(prefix))

    def root_docs(self):
        return dict((path, fields) for path, fields in self.firestore.docs.items()
                    if path.split("/")[0] in TOOL.COLLECTIONS)

    def test_the_collection_list_is_the_backups(self):
        backup = load_script("backup_operator_data", "backup-operator-data.py")
        self.assertEqual(TOOL.COLLECTIONS, backup.COLLECTIONS)

    def test_a_dry_run_writes_nothing(self):
        before = dict(self.firestore.docs)
        code, out = self.run_tool("--from", "root", "--to", "workspace:%s" % WID)
        self.assertEqual(code, 0)
        self.assertIn("--write would copy 10 documents", out)
        self.assertEqual(self.firestore.docs, before)

    def test_a_write_copies_every_document_exactly(self):
        code, out = self.run_tool("--from", "root", "--to", "workspace:%s" % WID, "--write")
        self.assertEqual(code, 0, out)
        self.assertIn("identical", out)
        self.assertEqual(self.workspace_docs(), self.root_docs())
        # Raw values, not a decode/encode round trip: types arrive unchanged.
        audit = self.workspace_docs()["audit/20261120T000000Z-ab"]
        self.assertEqual(audit, {"at": {"timestampValue": "2026-11-20T00:00:00Z"}})
        self.assertEqual(self.workspace_docs()["campaigns/web"]["ratio"], {"doubleValue": 0.5})

    def test_the_source_and_the_registry_are_untouched(self):
        before = dict(self.firestore.docs)
        self.run_tool("--from", "root", "--to", "workspace:%s" % WID, "--write")
        for path, fields in before.items():
            self.assertEqual(self.firestore.docs[path], fields, path)
        copied = {path.split("/")[0] for path in self.workspace_docs()}
        for foreign in ("users", "consoleUsers", "pendingGmailTokens", "workspaces"):
            self.assertNotIn(foreign, copied)

    def test_commits_are_addressed_to_the_database(self):
        self.run_tool("--from", "root", "--to", "workspace:%s" % WID, "--write")
        commits = [parent for _, verb, parent in self.firestore.requests if verb == ":commit"]
        self.assertEqual(commits, [""])

    def test_rewriting_is_idempotent(self):
        self.run_tool("--from", "root", "--to", "workspace:%s" % WID, "--write")
        code, out = self.run_tool("--from", "root", "--to", "workspace:%s" % WID, "--write")
        self.assertEqual(code, 0)
        self.assertIn("wrote 0 documents", out)

    def test_verify_catches_a_missing_a_changed_and_an_extra_document(self):
        self.run_tool("--from", "root", "--to", "workspace:%s" % WID, "--write")
        prefix = "workspaces/%s/" % WID
        del self.firestore.docs[prefix + "lines/default"]
        self.firestore.docs[prefix + "groups/1@g.us"] = {"status": {"stringValue": "on-hold"}}
        self.firestore.docs[prefix + "groups/2@g.us"] = {"status": {"stringValue": "active"}}
        code, out = self.run_tool("--from", "root", "--to", "workspace:%s" % WID, "--verify")
        self.assertEqual(code, 1)
        self.assertIn("missing default", out)
        self.assertIn("differ  1@g.us", out)
        self.assertIn("extra   2@g.us", out)
        self.assertIn("DIFFERENT", out)

    def test_an_integer_that_became_a_string_is_a_difference(self):
        self.run_tool("--from", "root", "--to", "workspace:%s" % WID, "--write")
        self.firestore.docs["workspaces/%s/lines/default" % WID]["rank"] = {"stringValue": "10"}
        code, _ = self.run_tool("--from", "root", "--to", "workspace:%s" % WID, "--verify")
        self.assertEqual(code, 1)

    def test_an_unknown_root_collection_stops_the_copy(self):
        self.firestore.docs["orders/1"] = {"x": {"stringValue": "y"}}
        code, _ = self.run_tool("--from", "root", "--to", "workspace:%s" % WID, "--write")
        self.assertEqual(code, 2)
        self.assertEqual(self.workspace_docs(), {})

    def test_the_rollback_direction_copies_a_workspace_back_to_the_root(self):
        self.run_tool("--from", "root", "--to", "workspace:%s" % WID, "--write")
        self.firestore.docs["workspaces/%s/groups/1@g.us" % WID] = {"status": {"stringValue": "on-hold"}}
        code, out = self.run_tool("--from", "workspace:%s" % WID, "--to", "root", "--write")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.firestore.docs["groups/1@g.us"], {"status": {"stringValue": "on-hold"}})

    def test_delete_source_needs_the_workspace_id_repeated(self):
        self.run_tool("--from", "root", "--to", "workspace:%s" % WID, "--write")
        code, _ = self.run_tool("--from", "root", "--to", "workspace:%s" % WID, "--delete-source")
        self.assertEqual(code, 2)
        code, _ = self.run_tool("--from", "root", "--to", "workspace:%s" % WID,
                                "--delete-source", "--confirm", "wk-other1")
        self.assertEqual(code, 2)
        self.assertIn("groups/1@g.us", self.firestore.docs)

    def test_delete_source_refuses_while_a_document_has_no_copy(self):
        self.run_tool("--from", "root", "--to", "workspace:%s" % WID, "--write")
        del self.firestore.docs["workspaces/%s/groups/1@g.us" % WID]
        code, out = self.run_tool("--from", "root", "--to", "workspace:%s" % WID,
                                  "--delete-source", "--confirm", WID)
        self.assertEqual(code, 1)
        self.assertIn("no copy", out)
        self.assertIn("groups/1@g.us", self.firestore.docs)

    def test_delete_source_removes_only_operator_data_once_everything_is_copied(self):
        self.run_tool("--from", "root", "--to", "workspace:%s" % WID, "--write")
        # The bot has moved on: a newer workspace copy is not a reason to refuse.
        self.firestore.docs["workspaces/%s/groups/1@g.us" % WID] = {"status": {"stringValue": "on-hold"}}
        code, out = self.run_tool("--from", "root", "--to", "workspace:%s" % WID,
                                  "--delete-source", "--confirm", WID)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.root_docs(), {})
        for kept in ("consoleUsers/uid-1", "workspaces/%s" % WID, "users/uid-1", "pendingGmailTokens/abc"):
            self.assertIn(kept, self.firestore.docs)
        self.assertEqual(len(self.workspace_docs()), 10)

    def test_bad_sides_are_refused(self):
        for argv in (["--from", "root", "--to", "root"],
                     ["--from", "root", "--to", "workspace:../x"],
                     ["--from", "elsewhere", "--to", "workspace:%s" % WID]):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_tool(*argv)[0], 2)


if __name__ == "__main__":
    unittest.main()
