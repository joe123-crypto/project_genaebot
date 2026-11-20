"""Seeding a new workspace with the settings and lines its bot cannot send without.

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

import operator_config as oc  # noqa: E402
from firestore_tree_fake import FakeTree  # noqa: E402

PROJECT = "genaie-test"
WID = "wk-beta123"

loader = importlib.machinery.SourceFileLoader("init_workspace", str(ROOT / "deploy" / "init-workspace.py"))
spec = importlib.util.spec_from_loader(loader.name, loader)
TOOL = importlib.util.module_from_spec(spec)
loader.exec_module(TOOL)

SETTINGS = {"version": 1, "_comment": "live", "activity": "normal", "caps_enabled": True,
            "rotation_enabled": False, "contact_account": "default",
            "bands": {"normal": [1, 3], "busy": [2, 5]}, "rank": 0}


class InitTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        key = os.path.join(self.dir, "key.json")
        Path(key).write_text(json.dumps({"project_id": PROJECT, "client_email": "seed@x",
                                         "private_key": "k"}))
        self.env = {"GCP_SERVICE_ACCOUNT_FILE": key}
        self.firestore = FakeTree(PROJECT, {"settings/defaults": oc.encode_fields(SETTINGS),
                                            "lines/default": oc.encode_fields({"number": "+26370000001"})})

    def run_tool(self, *argv):
        out = io.StringIO()
        code = TOOL.main(list(argv), self.env, opener=self.firestore, signer=lambda *a: "jwt",
                         now=lambda: 1_800_000_000.0, sleep=lambda s: None, out=out)
        return code, out.getvalue()

    def doc(self, path):
        fields = self.firestore.docs.get("workspaces/%s/%s" % (WID, path))
        return None if fields is None else oc.decode_fields(fields)

    def test_a_dry_run_writes_nothing(self):
        before = dict(self.firestore.docs)
        code, out = self.run_tool("--workspace", WID, "--line", "main=+263771234567")
        self.assertEqual(code, 0)
        self.assertIn("dry run", out)
        self.assertEqual(self.firestore.docs, before)

    def test_it_copies_the_running_settings_and_points_contact_at_the_new_line(self):
        code, out = self.run_tool("--workspace", WID, "--line", "main=+263771234567",
                                  "--line", "ads=+263771234568", "--write")
        self.assertEqual(code, 0, out)
        settings = self.doc("settings/defaults")
        self.assertEqual(settings["bands"], SETTINGS["bands"])
        self.assertEqual(settings["contact_account"], "main")
        self.assertEqual(self.doc("lines/main")["number"], "+263771234567")
        self.assertEqual(self.doc("lines/ads")["number"], "+263771234568")
        # The source install is untouched.
        self.assertEqual(oc.decode_fields(self.firestore.docs["settings/defaults"])["contact_account"],
                         "default")

    def test_the_seeded_workspace_loads_as_valid_config(self):
        self.run_tool("--workspace", WID, "--line", "main=+263771234567", "--write")
        env = {"GENAIE_FIRESTORE_DIR": os.path.join(self.dir, "fs"),
               "GCP_SERVICE_ACCOUNT_FILE": self.env["GCP_SERVICE_ACCOUNT_FILE"],
               "GENAIE_WORKSPACE_ID": WID}
        result = oc.load(env, opener=self.firestore, signer=lambda *a: "jwt",
                         now=lambda: 1_800_000_000.0, sleep=lambda s: None, log=lambda m: None)
        self.assertEqual(result["source"], "firestore")
        self.assertEqual(result["membership"]["defaults"]["accounts"],
                         {"main": {"number": "+263771234567"}})

    def test_it_never_overwrites_an_existing_workspace(self):
        self.firestore.docs["workspaces/%s/lines/main" % WID] = oc.encode_fields({"number": "+1"})
        code, _ = self.run_tool("--workspace", WID, "--line", "main=+263771234567", "--write")
        self.assertEqual(code, 2)
        self.assertEqual(self.doc("lines/main")["number"], "+1")
        self.assertIsNone(self.doc("settings/defaults"))

    def test_bad_input_is_refused(self):
        for argv in (["--workspace", WID],
                     ["--workspace", WID, "--line", "main=0771234567"],
                     ["--workspace", WID, "--line", "main=+263771234567", "--contact", "other"],
                     ["--workspace", "../x", "--line", "main=+263771234567"],
                     ["--workspace", WID, "--line", "main=+263771234567",
                      "--settings-from", "workspace:%s" % WID]):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_tool(*argv)[0], 2)


if __name__ == "__main__":
    unittest.main()
