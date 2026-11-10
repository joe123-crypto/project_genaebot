"""Converting a repository campaign into a data-defined one.

The conversion must change where a run reads its inputs and nothing about the
inputs, so every case here is one of: the document it writes carries exactly
what the script and manifest declared; something that would make the converted
campaign send differently is refused before any write; or the way back works.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes it.
"""

import hashlib
import os
import importlib.machinery
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT / "workspace/lib"), str(ROOT / "deploy")):
    if path not in sys.path:
        sys.path.insert(0, path)

import console_campaigns as cc  # noqa: E402
import operator_config as config  # noqa: E402
from test_console_campaigns import FakeGateway, MemoryStore  # noqa: E402


def tool_module():
    loader = importlib.machinery.SourceFileLoader(
        "migrate_campaigns", str(ROOT / "deploy" / "migrate-campaigns-to-firestore.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


TOOL = tool_module()
SCRIPT = '''
import os
HERE = os.path.dirname(os.path.abspath(__file__))
IMAGE = [os.path.join(HERE, "assets", "one.png"), os.path.join(HERE, "assets", "two.jpg")]
POST_TEXTS = ["Offer A {bot_number}", "Offer B {bot_number}"]
TRACKING = {"prefix": "GN", "label": "Genaie"}
if __name__ == "__main__":
    raise SystemExit("a conversion must never run a post script")
'''


def asset(slug, index, filename, enabled=True):
    digest = hashlib.sha256(filename.encode()).hexdigest()
    extension = filename.rsplit(".", 1)[1]
    return ("%s-%d" % (slug, index), {
        "campaign": slug, "filename": filename, "key": "ads/%s/%s.%s" % (slug, digest, extension),
        "sha256": digest, "bytes": 1000 + index, "enabled": enabled, "rank": index * 10,
        "content_type": "image/png" if extension == "png" else "image/jpeg"})


class Conversion(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "workspace"
        script = self.workspace / "cron" / "genaie-promo" / "post_ad.py"
        script.parent.mkdir(parents=True)
        script.write_text(SCRIPT, encoding="utf-8")
        self.entry = {"slug": "genaie", "name": "genaie-promo", "cron": "26 14 * * *",
                      "tz": "Africa/Harare", "failureAlertTo": "+10000000009",
                      "installer": "deploy/install-genaie-promo.sh",
                      "script": "/home/joseph/.openclaw/workspace/cron/genaie-promo/post_ad.py"}
        self.manifest = {"timeoutSeconds": 7200, "promos": [self.entry]}
        self.store = MemoryStore()
        self.index(asset("genaie", 1, "one.png"), asset("genaie", 2, "two.jpg"),
                   asset("genaie", 3, "retired.png", enabled=False), asset("cv", 1, "other.png"))
        self.out = io.StringIO()

    def index(self, *assets):
        self.store.docs["assets"] = dict((doc_id, config.encode_fields(fields))
                                         for doc_id, fields in assets)

    def convert(self, write=True):
        return TOOL.convert(self.entry, self.manifest, self.store, write=write, actor="tester",
                            workspace=self.workspace, out=self.out)

    def test_conversion_preserves_a_saved_start_time(self):
        doc = self.store.get("campaigns", "genaie")
        doc["scheduleOverride"] = {"cron": "21 14 * * *", "timezone": "Africa/Harare"}
        self.store.docs["campaigns"]["genaie"] = config.encode_fields(doc)
        self.convert()
        self.assertEqual(self.store.get("campaigns", "genaie")["schedule"]["time"], "14:21")

    def test_retired_campaign_cannot_be_converted_or_restored(self):
        self.store.docs["campaigns"]["genaie"] = config.encode_fields({"deleted": True, "enabled": False})
        with self.assertRaises(TOOL.MigrationError):
            self.convert()
        self.assertEqual(self.store.commits, [])
        self.assertTrue(self.store.get("campaigns", "genaie")["deleted"])

    def test_dry_run_reports_and_writes_nothing(self):
        before = json.dumps(self.store.docs, sort_keys=True)
        self.assertFalse(self.convert(write=False))
        self.assertEqual(json.dumps(self.store.docs, sort_keys=True), before)
        self.assertFalse(self.store.in_transaction)
        self.assertIn("genaie: 2 group(s), 14:26 Africa/Harare, 2 caption(s), 2 picture(s), job genaie-promo",
                      self.out.getvalue())

    def test_document_carries_what_the_script_and_manifest_declared(self):
        before = self.store.get("campaigns", "genaie")
        self.assertTrue(self.convert())
        doc = self.store.get("campaigns", "genaie")
        self.assertEqual(doc["kind"], "console")
        self.assertEqual(doc["name"], "Genaie")
        self.assertEqual(doc["captions"], ["Offer A {bot_number}", "Offer B {bot_number}"])
        self.assertEqual(doc["tracking"], {"prefix": "GN", "label": "Genaie"})
        self.assertEqual(doc["schedule"], {"time": "14:26", "timezone": "Africa/Harare",
                                           "weekdays": [0, 1, 2, 3, 4, 5, 6]})
        self.assertEqual((doc["timeoutSeconds"], doc["failureAlertTo"]), (7200, "+10000000009"))
        self.assertEqual((doc["jobName"], doc["scheduleStatus"]), ("genaie-promo", "pending"))
        # The enabled index entries only, in rotation order, as a run attaches them.
        self.assertEqual([image["filename"] for image in doc["images"]], ["one.png", "two.jpg"])
        self.assertTrue(all(image["key"].startswith("ads/genaie/") for image in doc["images"]))
        # Everything the operator owns is carried through untouched.
        for name in ("comment", "rank", "enabled"):
            self.assertEqual(doc[name], before[name])
        audit = [config.decode_fields(fields) for fields in self.store.docs["audit"].values()]
        self.assertEqual([(row["collection"], row["id"], row["actor"]) for row in audit],
                         [("campaigns", "genaie", "tester")])
        self.assertEqual(audit[0]["before"], before)

    def test_reconciler_then_repoints_the_existing_job_and_verify_passes(self):
        self.convert()
        self.assertFalse(TOOL.verify(self.entry, self.manifest, self.store, self.workspace, self.out))
        self.assertIn("not verified yet", self.out.getvalue())
        gateway = FakeGateway()
        gateway.jobs.append({"id": "legacy", "name": "genaie-promo", "enabled": True,
                             "schedule": {"expr": "26 14 * * *", "tz": "Africa/Harare"},
                             "payload": {"argv": ["python3", self.entry["script"]],
                                         "cwd": "/home/joseph/.openclaw/workspace", "timeoutSeconds": 7200}})
        from unittest.mock import patch
        with patch.dict("os.environ", {"OPENCLAW_STATE_DIR": self.temp.name}):
            cc.reconcile_one("genaie", self.store, call=gateway)
        self.assertEqual(len(gateway.jobs), 1)
        self.assertEqual(gateway.jobs[0]["payload"]["argv"][-1], "genaie")
        self.assertEqual(gateway.jobs[0]["schedule"], {"expr": "26 14 * * *", "tz": "Africa/Harare"})
        self.assertEqual(gateway.jobs[0]["failureAlert"], {"to": "+10000000009"})
        self.assertTrue(TOOL.verify(self.entry, self.manifest, self.store, self.workspace, self.out))

    def test_second_run_is_a_no_op_and_never_undoes_a_console_edit(self):
        self.convert()
        self.store.docs["campaigns"]["genaie"].update(config.encode_fields(
            {"captions": ["Edited in the console"], "enabled": False}))
        commits = len(self.store.commits)
        self.assertTrue(self.convert())
        self.assertEqual(len(self.store.commits), commits)
        self.assertEqual(self.store.get("campaigns", "genaie")["captions"], ["Edited in the console"])
        self.assertIn("already converted", self.out.getvalue())

    def test_existing_caption_override_wins_and_both_switches_must_be_on(self):
        self.store.docs["campaigns"]["genaie"].update(config.encode_fields({"captions": ["Console copy"]}))
        self.entry["enabled"] = False
        self.convert()
        doc = self.store.get("campaigns", "genaie")
        self.assertEqual(doc["captions"], ["Console copy"])
        self.assertFalse(doc["enabled"])

    def test_anything_that_would_change_what_is_sent_is_refused_before_a_write(self):
        cases = {
            "picture order": lambda: self.index(asset("genaie", 1, "two.jpg"), asset("genaie", 2, "one.png")),
            "missing picture": lambda: self.index(asset("genaie", 1, "one.png")),
            "on the hour": lambda: self.entry.update(cron="0 14 * * *"),
            "not daily": lambda: self.entry.update(cron="26 14 * * 1"),
            "short timeout": lambda: self.manifest.update(timeoutSeconds=120),
            "bad job name": lambda: self.entry.update(name="Genaie Promo"),
        }
        for label, breakage in cases.items():
            with self.subTest(label):
                self.setUp()
                breakage()
                with self.assertRaises(TOOL.MigrationError):
                    self.convert()
                self.assertEqual(self.store.commits, [])
                self.assertFalse(self.store.in_transaction)
                self.assertNotIn("kind", self.store.get("campaigns", "genaie"))

    def test_revert_hands_the_campaign_back_to_its_script(self):
        self.convert()
        self.assertTrue(TOOL.revert(self.entry, self.store, write=True, actor="tester", out=self.out))
        doc = self.store.get("campaigns", "genaie")
        self.assertEqual(doc["kind"], "manifest")
        self.assertNotIn("genaie", cc.definitions({"promos": {"genaie": doc}}))
        self.assertIn("deploy/install-genaie-promo.sh", self.out.getvalue())
        # And it can be converted again from there.
        self.assertTrue(self.convert())
        self.assertEqual(self.store.get("campaigns", "genaie")["kind"], "console")

    def test_every_declared_campaign_in_this_repository_is_convertible(self):
        """The real scripts and manifest, against an index built from the names
        each script itself declares: schedule, tracking, captions and job name
        all have to pass the same rules a console-created campaign passes."""
        manifest = TOOL.load_manifest()
        for entry in manifest["promos"]:
            with self.subTest(entry["slug"]):
                _captions, _tracking, filenames = TOOL.declared(entry)
                assets = [dict(fields, id=doc_id) for doc_id, fields in
                          (asset(entry["slug"], index + 1, name) for index, name in enumerate(filenames))]
                doc = TOOL.desired(entry, manifest, {"enabled": True}, assets)
                self.assertEqual(doc["jobName"], entry["name"])
                self.assertEqual(doc["enabled"], entry.get("enabled", True))
                self.assertGreaterEqual(len(doc["captions"]), 1)
                # State continuity is what stops the first converted run
                # re-posting today's ad: the script's state directory has to be
                # the one the converted campaign will open.
                module = TOOL.import_campaign(TOOL.script_path(entry["script"]))
                self.assertEqual(Path(module.STATE_FILE).parent,
                                 cc.state_directory(entry["slug"], doc))

    def test_selection_requires_known_campaigns(self):
        self.assertEqual(TOOL.selected(self.manifest, ["genaie"], False), [self.entry])
        with self.assertRaises(TOOL.MigrationError):
            TOOL.selected(self.manifest, ["nope"], False)


if __name__ == "__main__":
    unittest.main()
