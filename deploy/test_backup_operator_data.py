"""Backing up every operator document, and putting one back.

Once the groups, campaigns and captions live only in Firestore, this script is
the only copy of them that is not the live store, so the cases here are the ones
that decide whether it can be trusted: a backup is complete and self-checking, a
damaged or half-written one is refused rather than used, retention never eats the
newest, and a restore puts whole documents back without touching anything newer.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes it.
"""

import copy
import datetime
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT / "workspace/lib"), str(ROOT / "deploy")):
    if path not in sys.path:
        sys.path.insert(0, path)

import operator_config as config  # noqa: E402
import operator_writer  # noqa: E402
import r2_sigv4  # noqa: E402
from test_console_campaigns import MemoryStore  # noqa: E402


def tool_module():
    loader = importlib.machinery.SourceFileLoader(
        "backup_operator_data", str(ROOT / "deploy" / "backup-operator-data.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


tool = tool_module()

CREDS = {"project_id": "test-project", "client_email": "x@y", "private_key": "k"}
AT = datetime.datetime(2026, 11, 11, 8, 30, tzinfo=datetime.timezone.utc)


def key_env(directory):
    """An environment naming a real key file, so the credential path the script
    actually uses is exercised rather than stubbed out."""
    path = os.path.join(directory, "key.json")
    Path(path).write_text(json.dumps(CREDS), encoding="utf-8")
    return {"GCP_SERVICE_ACCOUNT_FILE": path}


def documents():
    """One document in each collection, enough to tell them apart in a file."""
    out = dict((collection, {}) for collection in tool.COLLECTIONS)
    out["settings"]["defaults"] = {"caps_enabled": True, "activity": "normal"}
    out["lines"]["default"] = {"number": "+26370000001"}
    out["campaigns"]["web-design"] = {"kind": "console", "enabled": True,
                                      "captions": ["hello"], "rank": 10}
    out["groups"]["120363000000000001@g.us"] = {"status": "active",
                                                "promos": ["web-design"], "rank": 10}
    out["assets"]["web-design-1"] = {"campaign": "web-design", "filename": "a.jpg",
                                     "enabled": True, "rank": 10}
    out["audit"]["20261111T000000Z-abc"] = {"actor": "joe", "collection": "groups"}
    out["campaign_creations"]["req-1"] = {"slug": "web-design", "digest": "d"}
    return out


def reader_for(docs):
    def reader(creds, cache_dir):
        return copy.deepcopy(docs)
    return reader


def no_bucket(env, *, endpoint=None, bucket=None):
    return None, "R2 is not configured: no R2_BUCKET or --bucket"


def bucket_with(objects):
    def reader(env, *, endpoint=None, bucket=None):
        return {"bucket": "host/agent-genaie", "objects": list(objects)}, None
    return reader


class BackupLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = os.path.join(self.temp.name, "backups")
        self.env = key_env(self.temp.name)
        self.docs = documents()

    def take(self, *, objects=None, no_r2=False, at=AT, retain=tool.DEFAULT_RETAIN):
        out = io.StringIO()
        return tool.backup(self.root, key=None, retain=retain, env=self.env,
                           no_r2=no_r2, now=lambda: at, out=out,
                           reader=reader_for(self.docs),
                           r2_reader=bucket_with(objects) if objects is not None else no_bucket)

    def test_a_backup_holds_every_collection_with_a_matching_manifest(self):
        manifest, directory = self.take(objects=[{"key": "ads/web-design/a.jpg",
                                                  "bytes": 12, "etag": "e", "modified": "t"}])
        self.assertEqual(os.path.basename(directory), "2026-11-11")
        self.assertEqual(manifest["project"], CREDS["project_id"])
        self.assertEqual(sorted(manifest["collections"]), sorted(tool.COLLECTIONS))
        for collection in tool.COLLECTIONS:
            name = "%s.json" % collection
            body = Path(directory, name).read_bytes()
            recorded = manifest["files"][name]
            self.assertEqual(recorded["sha256"], tool.digest(body),
                             "%s digest does not match the manifest" % name)
            self.assertEqual(recorded["documents"], len(self.docs[collection]))
            self.assertEqual(json.loads(body)["documents"], self.docs[collection])
        self.assertTrue(manifest["r2"]["listed"])
        self.assertEqual(manifest["r2"]["objects"], 1)

    def test_every_backed_up_file_is_private(self):
        _, directory = self.take(objects=[])
        for name in os.listdir(directory):
            mode = os.stat(os.path.join(directory, name)).st_mode & 0o077
            self.assertEqual(mode, 0, "%s is readable by other accounts" % name)

    def test_an_unlistable_bucket_is_recorded_not_fatal(self):
        # The weekly cron runs on the gateway, whose environment deliberately
        # has no bucket credentials; a Firestore-only backup must still happen.
        manifest, directory = self.take()
        self.assertFalse(manifest["r2"]["listed"])
        self.assertIn("R2_BUCKET", manifest["r2"]["reason"])
        self.assertFalse(Path(directory, tool.R2_NAME).exists())

    def test_no_r2_says_so_rather_than_naming_a_missing_variable(self):
        manifest, _ = self.take(objects=[], no_r2=True)
        self.assertEqual(manifest["r2"], {"listed": False, "reason": "--no-r2"})

    def test_a_second_backup_the_same_day_replaces_the_first(self):
        self.take(objects=[])
        self.docs["campaigns"]["second"] = {"kind": "console", "enabled": False}
        manifest, directory = self.take(objects=[])
        self.assertEqual(manifest["files"]["campaigns.json"]["documents"], 2)
        self.assertEqual(tool.backups(self.root), [directory])

    def test_a_bucket_that_cannot_be_reached_creates_nothing_at_all(self):
        # Both reads happen before anything is written, so a failure here must
        # not even leave an empty backup root behind to be mistaken for one.
        def explode(env, *, endpoint=None, bucket=None):
            raise r2_sigv4.R2Error("bucket on fire")
        with self.assertRaises(r2_sigv4.R2Error):
            tool.backup(self.root, key=None, retain=8, env=self.env, now=lambda: AT,
                        out=io.StringIO(), reader=reader_for(self.docs),
                        r2_reader=explode)
        self.assertFalse(os.path.exists(self.root))

    def test_a_backup_that_fails_while_writing_leaves_no_partial_directory(self):
        original = tool.assemble

        def explode(*args, **kwargs):
            # Write a file first, so there is something to clean up.
            original(*args, **kwargs)
            raise OSError("disk full")

        tool.assemble = explode
        self.addCleanup(setattr, tool, "assemble", original)
        with self.assertRaises(OSError):
            self.take(objects=[])
        self.assertEqual(tool.backups(self.root), [])
        self.assertEqual([name for name in os.listdir(self.root)
                          if name.startswith(".partial-")], [])
        self.assertFalse(os.path.exists(os.path.join(self.root, "2026-11-11")))


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = os.path.join(self.temp.name, "backups")
        os.makedirs(self.root)

    def plant(self, name, *, manifest=True):
        path = os.path.join(self.root, name)
        os.makedirs(path)
        if manifest:
            Path(path, tool.MANIFEST_NAME).write_text(
                json.dumps({"version": tool.VERSION, "collections": [], "files": {}}))
        return path

    def test_retention_keeps_the_newest_and_deletes_the_oldest(self):
        kept = [self.plant("2026-11-%02d" % day) for day in range(1, 6)]
        removed = tool.prune(self.root, 2, out=io.StringIO())
        self.assertEqual(removed, kept[:3])
        self.assertEqual(tool.backups(self.root), kept[3:])

    def test_zero_retention_keeps_everything(self):
        planted = [self.plant("2026-11-0%d" % day) for day in range(1, 4)]
        self.assertEqual(tool.prune(self.root, 0, out=io.StringIO()), [])
        self.assertEqual(tool.backups(self.root), planted)

    def test_anything_that_is_not_a_dated_backup_is_left_alone(self):
        # An operator's own copy, and a directory half-written by a crashed run.
        self.plant("keep-this-one")
        self.plant("2026-11-02", manifest=False)
        dated = self.plant("2026-11-03")
        self.assertEqual(tool.backups(self.root), [dated])
        tool.prune(self.root, 1, out=io.StringIO())
        self.assertTrue(os.path.isdir(os.path.join(self.root, "keep-this-one")))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "2026-11-02")))


class DamagedBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = os.path.join(self.temp.name, "backups")
        self.env = key_env(self.temp.name)
        _, self.directory = tool.backup(
            self.root, key=None, retain=8, env=self.env, now=lambda: AT,
            out=io.StringIO(), reader=reader_for(documents()), r2_reader=no_bucket)

    def test_a_file_that_no_longer_matches_its_digest_is_refused(self):
        path = Path(self.directory, "groups.json")
        path.write_text(json.dumps({"collection": "groups", "count": 0,
                                    "documents": {}}), encoding="utf-8")
        with self.assertRaises(tool.BackupError) as caught:
            tool.read_backup(self.directory, "groups")
        self.assertIn("damaged", str(caught.exception))

    def test_a_directory_without_a_manifest_is_not_a_backup(self):
        os.unlink(os.path.join(self.directory, tool.MANIFEST_NAME))
        with self.assertRaises(tool.BackupError) as caught:
            tool.read_backup(self.directory, "groups")
        self.assertIn("no manifest.json", str(caught.exception))

    def test_a_manifest_from_another_version_is_refused(self):
        path = Path(self.directory, tool.MANIFEST_NAME)
        manifest = json.loads(path.read_text())
        manifest["version"] = tool.VERSION + 1
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(tool.BackupError) as caught:
            tool.read_manifest(self.directory)
        self.assertIn("different version", str(caught.exception))


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = os.path.join(self.temp.name, "backups")
        self.env = key_env(self.temp.name)
        self.docs = documents()
        tool.backup(self.root, key=None, retain=8, env=self.env, now=lambda: AT,
                    out=io.StringIO(), reader=reader_for(self.docs), r2_reader=no_bucket)

    def check(self, live):
        out = io.StringIO()
        ok = tool.verify(self.root, key=None, env=self.env, out=out,
                         reader=reader_for(live))
        return ok, out.getvalue()

    def test_an_untouched_store_verifies(self):
        ok, text = self.check(self.docs)
        self.assertTrue(ok)
        self.assertIn("matches Firestore", text)

    def test_a_changed_field_is_named(self):
        live = copy.deepcopy(self.docs)
        live["campaigns"]["web-design"]["enabled"] = False
        ok, text = self.check(live)
        self.assertFalse(ok)
        self.assertIn("campaigns/web-design differs in enabled", text)

    def test_documents_on_only_one_side_are_named_in_the_right_direction(self):
        live = copy.deepcopy(self.docs)
        del live["groups"]["120363000000000001@g.us"]
        live["groups"]["120363000000000002@g.us"] = {"status": "active", "promos": []}
        ok, text = self.check(live)
        self.assertFalse(ok)
        self.assertIn("groups/120363000000000001@g.us is in the backup but not in Firestore", text)
        self.assertIn("groups/120363000000000002@g.us is in Firestore but not in the backup", text)

    def test_verifying_with_no_backup_says_so(self):
        with self.assertRaises(tool.BackupError) as caught:
            tool.verify(os.path.join(self.temp.name, "empty"), key=None, env=self.env,
                        out=io.StringIO(), reader=reader_for(self.docs))
        self.assertIn("no backup", str(caught.exception))


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = os.path.join(self.temp.name, "backups")
        self.env = key_env(self.temp.name)
        self.store = MemoryStore()
        saved = dict((collection, {}) for collection in tool.COLLECTIONS)
        saved["groups"] = dict(
            (doc_id, config.decode_fields(fields))
            for doc_id, fields in self.store.list("groups"))
        self.saved = saved
        _, self.directory = tool.backup(
            self.root, key=None, retain=8, env=self.env, now=lambda: AT,
            out=io.StringIO(), reader=reader_for(saved), r2_reader=no_bucket)
        self.first = sorted(self.saved["groups"])[0]

    def restore(self, collection="groups", *, write=False):
        out = io.StringIO()
        tool.restore(self.directory, collection, key=None, write=write,
                     actor="tester", env=self.env, store=self.store, out=out)
        return out.getvalue()

    def test_a_dry_run_names_the_changes_and_writes_nothing(self):
        self.store.docs["groups"][self.first] = config.encode_fields({"status": "on-hold"})
        text = self.restore()
        self.assertIn("1 document(s) to put back", text)
        self.assertIn("dry run: nothing written", text)
        self.assertEqual(self.store.commits, [])

    def test_a_write_puts_whole_documents_back_with_one_audit_document(self):
        self.store.docs["groups"][self.first] = config.encode_fields({"status": "on-hold"})
        self.restore(write=True)
        self.assertEqual(self.store.get("groups", self.first), self.saved["groups"][self.first])
        written = dict(((collection, doc_id), fields)
                       for collection, doc_id, fields, _ in self.store.commits[-1])
        audits = [key for key in written if key[0] == operator_writer.AUDIT_COLLECTION]
        self.assertEqual(len(audits), 1)
        self.assertEqual(written[audits[0]]["after"]["restored"], 1)

    def test_a_document_newer_than_the_backup_is_left_alone(self):
        self.store.docs["groups"]["120363000000000009@g.us"] = config.encode_fields(
            {"status": "active", "promos": []})
        text = self.restore(write=True)
        self.assertIn("1 newer left alone", text)
        self.assertIsNotNone(self.store.get("groups", "120363000000000009@g.us"))

    def test_an_unchanged_collection_commits_nothing(self):
        self.restore(write=True)
        self.assertEqual(self.store.commits, [])

    def test_a_collection_that_is_not_backed_up_is_refused(self):
        with self.assertRaises(tool.BackupError) as caught:
            self.restore("whatever")
        self.assertIn("not a backed-up collection", str(caught.exception))


class CommandLineTests(unittest.TestCase):
    def test_restore_needs_a_collection_and_write_needs_a_restore(self):
        for argv in (["--restore", "/tmp/x"], ["--collection", "groups"],
                     ["--write"]):
            with self.assertRaises(SystemExit):
                tool.main(["backup-operator-data.py"] + argv, env={})

    def test_a_missing_key_is_bad_input_not_a_crash(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        code = tool.main(["backup-operator-data.py", "--out", temp.name,
                          "--no-r2", "--no-env-file"], env={})
        self.assertEqual(code, 2)

    def test_the_default_root_is_runtime_backups_on_the_volume(self):
        root = tool.default_root({"OPENCLAW_STATE_DIR": "/state"})
        self.assertEqual(root, os.path.join("/state", "runtime", "backups"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
