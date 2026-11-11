"""Behavior and request budgets for targeted Firestore operations (no network)."""
import os
import importlib.machinery
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import operator_config as config
import console_config
import operator_writer as writer
import promo_caps
import console_campaigns
from test_operator_config import Harness, JID_A, JID_B, JID_C
from test_console_campaigns import MemoryStore, ENV, payload


class ScopedReads(Harness):
    def scoped(self, **scope):
        return self.load(**scope)

    def test_campaign_queries_exclude_unrelated_groups_without_changing_caps(self):
        for i in range(500):
            self.firestore.store["groups"]["%s@g.us" % (1000000 + i)] = config.encode_fields({
                "status": "active", "promos": ["cv"], "name": "Unrelated %s" % i})
        full = self.load()
        self.firestore.requests.clear()
        result = self.scoped(campaign="genaie")
        self.assertEqual(set(result["membership"]["groups"]), {JID_A, JID_B})
        self.assertFalse(any("/documents/groups?" in url for url in self.firestore.requests))
        self.assertTrue(any(url.endswith(":runQuery") for url in self.firestore.requests))
        self.assertTrue(any("/settings/defaults" in url for url in self.firestore.requests))
        ids = list(result["membership"]["groups"])
        self.assertEqual(promo_caps.plan(full["membership"], ids, refs={}, history={}, disabled=[]),
                         promo_caps.plan(result["membership"], ids, refs={}, history={}, disabled=[]))

    def test_weekly_only_and_paused_groups_are_not_lost(self):
        result = self.scoped(campaign="cv")
        self.assertIn(JID_B, result["membership"]["groups"])
        self.assertNotIn(JID_A, result["membership"]["groups"])
        self.firestore.store["groups"][JID_B]["paused_promos"] = config.encode_value(["cv"])
        self.assertIn(JID_B, self.scoped(campaign="cv")["membership"]["groups"])

    def test_point_lookup_and_settings_never_scan_group_collection(self):
        result = self.scoped(group_ids=[JID_A, JID_A, "999@g.us"])
        self.assertEqual(list(result["membership"]["groups"]), [JID_A])
        self.assertEqual(sum("/groups/" in url for url in self.firestore.requests), 2)
        self.assertFalse(any("/documents/groups?" in url for url in self.firestore.requests))
        self.firestore.requests.clear()
        self.assertEqual(self.scoped(include_groups=False, include_campaigns=False)["membership"]["groups"], {})
        self.assertFalse(any("/groups" in url or url.endswith(":runQuery") for url in self.firestore.requests))
        self.assertFalse(any("/campaigns" in url for url in self.firestore.requests))

    def test_scopes_never_replace_full_snapshots_and_fallback_is_bounded(self):
        full = self.load()
        path = config.snapshot_path(config.state_dir(self.env))
        original = Path(path).read_text()
        self.scoped(campaign="cv")
        self.assertEqual(Path(path).read_text(), original)
        self.firestore.down = True
        # A different scope can reuse a full disk snapshot without any live scan.
        result = self.scoped(group_ids=[JID_C])
        self.assertEqual(list(result["membership"]["groups"]), [JID_C])
        self.assertEqual(result["fetchedAt"], full["fetchedAt"])
        self.clock[0] += config.SNAPSHOT_MAX_AGE_SECONDS + 1
        with self.assertRaises(config.ConfigUnavailable):
            self.scoped(group_ids=[JID_C])

    def test_display_scopes_share_work_and_local_invalidations(self):
        def display(**scope):
            return console_config.load(self.env, opener=self.firestore, signer=self.signer,
                now=lambda: self.clock[0], sleep=lambda s: None, log=self.logs.append, **scope)
        display(campaign="genaie")
        count = len(self.firestore.requests)
        display(campaign="genaie")
        self.assertEqual(len(self.firestore.requests), count)
        other = display(campaign="cv")
        self.assertNotIn(JID_A, other["membership"]["groups"])
        console_config.invalidate(self.env)
        before = len(self.firestore.requests)
        display(campaign="genaie")
        self.assertGreater(len(self.firestore.requests), before)

    def test_aggregation_returns_counts_without_document_payloads(self):
        count = config.count_documents("test", "groups", "tok-test", where={"fieldFilter": {
            "field": {"fieldPath": "status"}, "op": "EQUAL", "value": {"stringValue": "on-hold"}}},
            opener=self.firestore, sleep=lambda s: None)
        self.assertEqual(count, 1)
        self.assertEqual(len(self.firestore.requests), 1)
        self.assertTrue(self.firestore.requests[0].endswith(":runAggregationQuery"))


class GroupBatch(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.reads = []
        original = self.store.get
        self.store.get = lambda collection, key: (self.reads.append((collection, key)), original(collection, key))[1]
        self.lists = []
        listing = self.store.list
        self.store.list = lambda collection: (self.lists.append(collection), listing(collection))[1]

    def write(self, changes, **kw):
        return writer.write_group_batch(changes, "Operator", store=self.store, **kw)

    def test_batch_reads_only_selected_groups_and_commits_audits_together(self):
        result = self.write([{"id": JID_A, "fields": {"note": "one"}}, {"id": JID_B, "fields": {"note": "two"}}])
        self.assertEqual(result, {"done": [JID_A, JID_B], "changed": 2})
        self.assertEqual(self.lists, ["lines", "campaigns"])
        self.assertEqual(self.reads, [("settings", "defaults"), ("groups", JID_A), ("groups", JID_B)])
        self.assertEqual(len(self.store.commits), 1)
        self.assertEqual(len(self.store.commits[0]), 4)
        self.assertEqual(sum(doc[0] == "audit" for doc in self.store.commits[0]), 2)

    def test_validation_conflict_and_dry_run_never_commit(self):
        bad = [{"id": JID_A, "fields": {"note": "valid"}}, {"id": JID_B, "fields": {"status": "bad"}}]
        with self.assertRaises(writer.WriteRefused):
            self.write(bad)
        self.assertEqual(self.store.commits, [])
        with self.assertRaises(writer.WriteRefused):
            self.write([{"id": JID_A, "fields": {"promos": ["cv"]}, "expected": {"promos": ["cv"]}}])
        self.assertEqual(self.store.commits, [])
        self.write([{"id": JID_A, "fields": {"note": "check"}}], dry_run=True)
        self.assertEqual(self.store.commits, [])

    def test_noops_and_invalid_ids_have_no_writes(self):
        result = self.write([{"id": JID_A, "fields": {"name": "Alpha"}}])
        self.assertEqual(result["changed"], 0)
        self.assertEqual(self.store.commits, [])
        for changes in ([], [{"id": "bad", "fields": {}}], [{"id": JID_A, "fields": {}}] * 2):
            with self.assertRaises(writer.WriteRefused):
                self.write(changes)

    def test_legacy_whatsapp_group_ids_remain_supported(self):
        legacy = "263786627810-1599925488@g.us"
        self.store.docs["groups"][legacy] = self.store.docs["groups"].pop(JID_A)
        self.assertEqual(self.write([{"id": legacy, "fields": {"note": "legacy"}}])["changed"], 1)

    def test_campaign_creation_reads_only_selected_groups(self):
        console_campaigns.create(payload(), self.store, ENV, verify=lambda *_: None, reconcile=lambda *_: None)
        self.assertEqual(self.lists, ["lines", "campaigns"])
        self.assertEqual([key for collection, key in self.reads if collection == "groups"], [JID_A, JID_B])
        self.assertEqual(len(self.store.commits), 1)

    def test_lost_commit_acknowledgement_is_never_reported_as_nothing_saved(self):
        self.store.fail_commit = True
        with self.assertRaises(writer.WriteRefused) as caught:
            self.write([{"id": JID_A, "fields": {"note": "changed"}}])
        self.assertEqual(caught.exception.status, 503)
        self.assertIn("outcome is unknown", str(caught.exception))


class SchedulerReads(unittest.TestCase):
    def test_store_filters_console_campaigns_in_firestore(self):
        store = object.__new__(console_campaigns.Store)
        store.transaction = None
        store.request = Mock(return_value=[])
        where = {"fieldFilter": {"field": {"fieldPath": "kind"}, "op": "EQUAL", "value": {"stringValue": "console"}}}
        self.assertEqual(store.list("campaigns", where=where), [])
        store.request.assert_called_once_with(":runQuery", {"structuredQuery": {
            "from": [{"collectionId": "campaigns"}], "where": where}})

    def test_repeated_pending_failures_do_not_rewrite_status(self):
        store = MemoryStore()
        # One campaign, so the count is about the retry and not about how many
        # campaigns the shared fixture happens to carry.
        store.docs["campaigns"] = {
            "demo": config.encode_fields({"kind": "console", "scheduleStatus": "pending"})}
        listing = store.list
        store.list = Mock(side_effect=lambda collection, where=None: listing(collection))
        store.request = Mock()
        with patch.object(console_campaigns, "reconcile_one", side_effect=RuntimeError("offline")), patch("sys.stderr"):
            self.assertEqual(console_campaigns.reconcile_all(store), 1)
            self.assertEqual(console_campaigns.reconcile_all(store), 1)
        store.request.assert_not_called()
        self.assertEqual(store.list.call_args.kwargs["where"]["fieldFilter"]["value"], {"stringValue": "console"})


class CampaignPreflightReads(unittest.TestCase):
    def test_switch_and_caption_checks_read_only_one_campaign_document(self):
        path = Path(__file__).resolve().parents[1] / "bin" / "console-read"
        loader = importlib.machinery.SourceFileLoader("scoped_console_read", str(path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        bridge = importlib.util.module_from_spec(spec)
        loader.exec_module(bridge)
        with patch.object(config, "credentials", return_value={"project_id": "test"}), \
             patch.object(config, "access_token", return_value="tok"), \
             patch.object(config, "get_document", return_value={
                 "kind": "console", "scheduleStatus": "ready",
                 "enabled": False, "captions": ["Saved text"]}) as get, \
             patch.object(bridge, "resolved", side_effect=AssertionError("full config read")):
            self.assertFalse(bridge.campaign_edit_state("demo")["campaign"]["storeEnabled"])
            self.assertEqual(bridge.campaign_edit_state("demo", captions_only=True)["captions"], ["Saved text"])
            self.assertEqual(get.call_count, 2)
            self.assertEqual(get.call_args.args, ("test", "campaigns", "demo", "tok"))


if __name__ == "__main__":
    unittest.main()
