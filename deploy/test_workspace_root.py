"""A bot pinned to a workspace reads, writes and caches only that workspace.

``GENAIE_WORKSPACE_ID`` moves every document path from the root collections to
``workspaces/<id>/``. The cases here are the ones that decide whether two
operators' data can ever meet: unset changes nothing, set prefixes every path
(and leaves database-wide calls on the database), a snapshot or cache filled
from one tree is never served as the other, and pictures get their own prefix.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes it. Fully offline.
"""

import contextlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT / "workspace/lib"), str(ROOT / "deploy")):
    if path not in sys.path:
        sys.path.insert(0, path)

import ad_assets  # noqa: E402
import console_campaigns  # noqa: E402
import console_config  # noqa: E402
import console_live_index  # noqa: E402
import operator_config as oc  # noqa: E402
import operator_writer  # noqa: E402
from firestore_tree_fake import FakeTree  # noqa: E402
from test_operator_config import JID_A, JID_B, creds_b64, membership, seed_store  # noqa: E402

PROJECT = "genaie-test"
WID = "wk-alpha1"
SHA = "a" * 64


def tree_with(prefix, store):
    return dict(("%s%s/%s" % (prefix, collection, doc_id), fields)
                for collection, docs in store.items() for doc_id, fields in docs.items())


def other_membership():
    """A second operator's config: one group the first does not have."""
    m = membership()
    m["groups"] = {JID_A: dict(m["groups"][JID_A], name="Other operator's group")}
    return m


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root_env = {"GENAIE_FIRESTORE_DIR": os.path.join(self.dir, "fs"),
                         "GCP_SERVICE_ACCOUNT_B64": creds_b64(),
                         "GENAIE_AD_ASSETS_BASE_URL": "https://assets.example.test"}
        self.ws_env = dict(self.root_env, GENAIE_WORKSPACE_ID=WID)
        docs = tree_with("", seed_store())
        docs.update(tree_with("workspaces/%s/" % WID, seed_store(other_membership())))
        self.firestore = FakeTree(PROJECT, docs)

    def load(self, env, **kw):
        return oc.load(env, opener=self.firestore, signer=lambda *a: "jwt",
                       now=lambda: 1_800_000_000.0, sleep=lambda s: None,
                       log=lambda message: None, **kw)


class PathTests(unittest.TestCase):
    def test_unset_is_exactly_the_single_operator_path(self):
        self.assertEqual(oc.documents_url(PROJECT, "groups", "1@g.us", env={}),
                         "https://firestore.googleapis.com/v1/projects/genaie-test/databases/"
                         "(default)/documents/groups/1%40g.us")
        self.assertEqual(oc.root_label({}), "root")

    def test_set_puts_every_document_under_the_workspace(self):
        env = {"GENAIE_WORKSPACE_ID": WID}
        self.assertEqual(oc.documents_url(PROJECT, "groups", "1@g.us", env=env),
                         "https://firestore.googleapis.com/v1/projects/genaie-test/databases/"
                         "(default)/documents/workspaces/wk-alpha1/groups/1%40g.us")
        self.assertEqual(oc.documents_root(PROJECT, env),
                         "projects/genaie-test/databases/(default)/documents/workspaces/wk-alpha1")
        self.assertEqual(oc.root_label(env), "workspaces/wk-alpha1")

    def test_database_wide_calls_never_move(self):
        self.assertEqual(oc.database_documents(PROJECT),
                         "projects/genaie-test/databases/(default)/documents")

    def test_a_malformed_workspace_id_fails_closed_rather_than_reading_the_root(self):
        for bad in ("abc", "x" * 41, "../groups", "wk/alpha1", "wk alpha1", "wk-é1234"):
            with self.subTest(bad=bad), self.assertRaises(oc.ConfigUnavailable):
                oc.documents_url(PROJECT, "groups", env={"GENAIE_WORKSPACE_ID": bad})

    def test_surrounding_whitespace_is_not_a_different_workspace(self):
        self.assertEqual(oc.workspace_id({"GENAIE_WORKSPACE_ID": " %s\n" % WID}), WID)
        self.assertIsNone(oc.workspace_id({"GENAIE_WORKSPACE_ID": "  "}))


class ReadTests(Base):
    def test_each_tree_reads_only_its_own_groups(self):
        root = self.load(self.root_env)
        workspace = self.load(self.ws_env)
        self.assertEqual(sorted(root["membership"]["groups"]), sorted(membership()["groups"]))
        self.assertEqual(list(workspace["membership"]["groups"]), [JID_A])
        self.assertEqual(workspace["membership"]["groups"][JID_A]["name"], "Other operator's group")

    def test_every_request_a_workspace_read_makes_stays_in_the_workspace(self):
        self.load(self.ws_env)
        self.load(self.ws_env, campaign="genaie")
        self.load(self.ws_env, group_ids=[JID_A, JID_B])
        for method, path, parent in self.firestore.requests:
            where = parent if parent is not None else path
            with self.subTest(request=(method, path, parent)):
                self.assertTrue(where.startswith("workspaces/%s" % WID), where)

    def test_a_campaign_query_and_a_count_use_the_workspace_as_parent(self):
        result = self.load(self.ws_env, campaign="genaie")
        self.assertEqual(list(result["membership"]["groups"]), [JID_A])
        token = "tok"
        count = oc.count_documents(PROJECT, "groups", token, opener=self.firestore,
                                   sleep=lambda s: None, env=self.ws_env)
        self.assertEqual(count, 1)


class SnapshotTests(Base):
    def down(self):
        def refuse(request, timeout=None):
            raise urllib.error.URLError("connection refused")
        self.firestore = refuse

    def test_a_snapshot_records_the_tree_it_was_read_from(self):
        self.load(self.ws_env)
        path = oc.snapshot_path(oc.state_dir(self.ws_env))
        self.assertEqual(json.loads(Path(path).read_text())["root"], "workspaces/%s" % WID)

    def test_a_root_snapshot_is_never_a_workspace_fallback(self):
        self.load(self.root_env)
        self.down()
        with self.assertRaises(oc.ConfigUnavailable) as caught:
            self.load(self.ws_env)
        self.assertIn("was read from root", str(caught.exception))

    def test_a_workspace_snapshot_is_never_a_root_fallback(self):
        self.load(self.ws_env)
        self.down()
        with self.assertRaises(oc.ConfigUnavailable):
            self.load(self.root_env)

    def test_the_same_tree_still_falls_back_to_its_snapshot(self):
        self.load(self.ws_env)
        self.down()
        result = self.load(self.ws_env)
        self.assertEqual(result["source"], "snapshot")
        self.assertEqual(list(result["membership"]["groups"]), [JID_A])

    def test_a_snapshot_from_before_workspaces_still_serves_the_root(self):
        self.load(self.root_env)
        path = oc.snapshot_path(oc.state_dir(self.root_env))
        snapshot = json.loads(Path(path).read_text())
        del snapshot["root"]
        Path(path).write_text(json.dumps(snapshot))
        self.down()
        self.assertEqual(self.load(self.root_env)["source"], "snapshot")

    def test_moving_trees_does_not_log_every_document_as_changed(self):
        self.load(self.root_env)
        self.load(self.ws_env)
        logs = list(Path(oc.state_dir(self.ws_env)).glob("changes-*.jsonl"))
        self.assertEqual(logs, [])

    def test_scoped_caches_are_separate_per_tree(self):
        self.assertNotEqual(oc.scope_directory(self.root_env, campaign="genaie"),
                            oc.scope_directory(self.ws_env, campaign="genaie"))


class WriteTests(Base):
    def test_a_commit_goes_to_the_database_and_names_the_workspace(self):
        operator_writer.commit(PROJECT, "tok", [("groups", "9@g.us", {"name": "new"})], [],
                               opener=self.firestore, sleep=lambda s: None, env=self.ws_env)
        self.assertIn("workspaces/%s/groups/9@g.us" % WID, self.firestore.docs)
        self.assertNotIn("groups/9@g.us", self.firestore.docs)
        self.assertIn(("POST", ":commit", ""), self.firestore.requests)

    def test_the_transactional_store_reads_the_workspace_and_commits_to_the_database(self):
        with mock.patch("urllib.request.urlopen", self.firestore), \
                mock.patch.object(oc, "node_signer", lambda *a: "jwt"):
            store = console_campaigns.Store(self.ws_env)
            store.begin()
            self.assertEqual(store.get("groups", JID_A)["name"], "Other operator's group")
            self.assertIsNone(store.get("groups", JID_B))
            store.commit([("groups", "9@g.us", {"name": "new"}, False)])
        self.assertIn("workspaces/%s/groups/9@g.us" % WID, self.firestore.docs)
        calls = [(verb, parent) for _, verb, parent in self.firestore.requests if parent is not None]
        self.assertIn((":beginTransaction", ""), calls)
        self.assertIn((":commit", ""), calls)

    def test_a_root_store_behaves_as_before(self):
        with mock.patch("urllib.request.urlopen", self.firestore), \
                mock.patch.object(oc, "node_signer", lambda *a: "jwt"):
            store = console_campaigns.Store(self.root_env)
            self.assertEqual(store.get("groups", JID_B)["name"], "Bravo")
            store.commit([("groups", "9@g.us", {"name": "new"}, False)])
        self.assertIn("groups/9@g.us", self.firestore.docs)


class PictureTests(Base):
    def test_a_workspace_bot_keeps_pictures_under_its_own_tree(self):
        self.assertEqual(ad_assets.key_prefix({"GENAIE_WORKSPACE_ID": WID}),
                         "workspaces/%s/campaigns/" % WID)

    def test_a_bot_without_a_workspace_has_nowhere_to_put_pictures(self):
        with self.assertRaises(ad_assets.AssetsUnavailable):
            ad_assets.key_prefix({})
        with self.assertRaises(ad_assets.AssetsUnavailable):
            ad_assets.key_prefix({"GENAIE_WORKSPACE_ID": "../x"})

    def test_the_old_prefix_variable_is_ignored(self):
        # It pinned the oldest install to ads/; the layout is no longer a setting.
        for old in ("ads/", "w/%s/ads/" % WID, "pictures/"):
            env = {"GENAIE_WORKSPACE_ID": WID, "GENAIE_ASSET_KEY_PREFIX": old}
            self.assertEqual(ad_assets.key_prefix(env), "workspaces/%s/campaigns/" % WID)

    def test_stored_keys_of_the_workspace_and_legacy_shapes_are_pinned(self):
        for good in ("workspaces/%s/campaigns/cv/%s.jpg" % (WID, SHA),
                     "ads/cv/%s.jpg" % SHA, "w/%s/ads/cv/%s.png" % (WID, SHA)):
            self.assertTrue(ad_assets.KEY_RE.match(good), good)
        for bad in ("w/../ads/cv/%s.jpg" % SHA, "//evil.example/ads/cv/%s.jpg" % SHA,
                    "w/%s/x/ads/cv/%s.jpg" % (WID, SHA), "pictures/cv/%s.jpg" % SHA,
                    "workspaces/../campaigns/cv/%s.jpg" % SHA,
                    "workspaces/%s/cv/%s.jpg" % (WID, SHA),
                    "workspaces/%s/campaigns/%s.jpg" % (WID, SHA),
                    "workspaces/%s/ads/cv/%s.jpg" % (WID, SHA)):
            self.assertFalse(ad_assets.KEY_RE.match(bad), bad)

    def test_a_root_picture_index_is_never_a_workspace_fallback(self):
        index = lambda env, opener: ad_assets.index(  # noqa: E731
            env, opener=opener, signer=lambda *a: "jwt", now=lambda: 1_800_000_000.0,
            sleep=lambda s: None, log=lambda message: None)
        index(self.root_env, self.firestore)
        def refuse(request, timeout=None):
            raise urllib.error.URLError("connection refused")
        with self.assertRaises(ad_assets.AssetsUnavailable) as caught:
            index(self.ws_env, refuse)
        self.assertIn("was read from root", str(caught.exception))
        self.assertEqual(index(self.root_env, refuse)["source"], "snapshot")


class DisplayCacheTests(Base):
    def test_the_console_cache_identity_includes_the_tree(self):
        self.assertNotEqual(console_config._identity(self.root_env),
                            console_config._identity(self.ws_env))

    def test_the_live_index_discards_another_trees_rows(self):
        first = console_live_index.Index(self.root_env, stream=lambda index: None)
        with first.db() as db:
            db.execute("INSERT INTO docs VALUES((SELECT generation FROM meta WHERE id=1),"
                       "'groups','1@g.us','{}')")
        second = console_live_index.Index(self.ws_env, stream=lambda index: None)
        self.assertEqual(second.path, first.path)
        with contextlib.closing(sqlite3.connect(second.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM docs").fetchone()[0], 0)
        self.assertNotEqual(console_live_index.identity(self.root_env),
                            console_live_index.identity(self.ws_env))


def sdk_present():
    try:
        import google.cloud.firestore_v1  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(sdk_present(), "google-cloud-firestore is not installed")
class ListenTargetTests(Base):
    def test_the_stream_listens_under_the_workspace_on_the_same_database(self):
        captured = []

        class Responses:
            def __iter__(self):
                return iter(())

            def cancel(self):
                pass

        def listen(*, requests, metadata, retry):
            iterator = iter(requests)
            for _ in console_live_index.COLLECTIONS:
                captured.append(next(iterator))
            self.assertEqual(metadata, (("google-cloud-resource-prefix",
                                         "projects/%s/databases/(default)" % PROJECT),))
            return Responses()

        client = mock.Mock()
        client.listen.side_effect = listen
        index = console_live_index.Index(self.ws_env, stream=lambda index: None)
        index.lease_until = index.clock() + 90
        with mock.patch("google.cloud.firestore_v1.services.firestore.FirestoreClient",
                        return_value=client), \
                mock.patch("google.oauth2.service_account.Credentials.from_service_account_info",
                           return_value=mock.Mock()):
            console_live_index.firestore_stream(index)
        self.assertEqual(len(captured), len(console_live_index.COLLECTIONS))
        for request in captured:
            self.assertEqual(request.database, "projects/%s/databases/(default)" % PROJECT)
            self.assertEqual(request.add_target.query.parent,
                             "projects/%s/databases/(default)/documents/workspaces/%s" % (PROJECT, WID))


if __name__ == "__main__":
    unittest.main()
