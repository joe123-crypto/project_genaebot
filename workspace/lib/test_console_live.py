"""Live display budgets and durable synchronization behavior, without network."""
import datetime
import importlib.machinery
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(__file__))
import console_live_index as live
import console_live_api as api
import operator_config as config
from test_operator_config import Harness, JID_A, JID_B, JID_C


class LiveIndexTests(Harness):
    def setUp(self):
        super().setUp()
        self.env["GENAIE_CONFIG_SOURCE"] = "firestore"
        self.index = live.Index(self.env, clock=lambda: self.clock[0])
        self.addCleanup(self.index.close)
        self.index.touch = Mock()

    def seed(self):
        changes = [(collection, name, fields) for collection, docs in self.firestore.store.items()
                   if collection in live.COLLECTIONS for name, fields in docs.items()]
        self.index.publish(changes, "2026-11-10T10:00:00Z", {"1": "resume"}, reset=True)

    def service(self):
        self.seed()
        view = api.LiveViews(Path(__file__).resolve().parents[1] / "bin/console-read", self.env, index=self.index)
        self.addCleanup(view.close)
        return view

    def test_bootstrap_is_unavailable_until_atomic_publish_and_survives_restart(self):
        with self.assertRaises(live.NotReady):
            self.index.snapshot()
        self.seed()
        restarted = live.Index(self.env)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.snapshot()[0]["membership"]["groups"][JID_A]["name"], "Alpha")
        self.assertEqual(restarted.checkpoint()["tokens"], {"1": "resume"})

    def test_incremental_change_delete_and_resume_position_publish_together(self):
        self.seed()
        self.index.publish([("groups", JID_C, None)], "2026-11-10T10:01:00Z", {"1": "next"})
        self.assertNotIn(JID_C, self.index.snapshot()[0]["membership"]["groups"])
        self.assertEqual(self.index.checkpoint()["tokens"], {"1": "next"})
        before = self.index.checkpoint()
        with self.assertRaises(config.ConfigInvalid):
            self.index.publish([("settings", "defaults", None)], "2026-11-10T10:02:00Z", {"1": "bad"})
        self.assertEqual(self.index.checkpoint(), before)
        self.assertIn(JID_A, self.index.snapshot()[0]["membership"]["groups"])

    def test_retired_campaign_does_not_reappear_in_stale_schedule_or_lists(self):
        self.seed()
        self.index.publish([("campaigns", "cv", config.encode_fields({"deleted": True, "enabled": False}))],
                           "2026-11-10T10:01:00Z", {})
        result, _ = self.index.snapshot()
        self.assertIn("cv", result["deleted"])
        self.assertNotIn("cv", result["membership"]["promos"])

    def test_reset_replaces_generation_without_old_documents_or_resume_tokens(self):
        self.seed()
        changes = [(collection, name, fields) for collection, docs in self.firestore.store.items()
                   if collection in live.COLLECTIONS for name, fields in docs.items() if name != JID_C]
        self.index.publish(changes, "2026-11-10T10:03:00Z", {"2": "reset"}, reset=True)
        self.assertNotIn(JID_C, self.index.snapshot()[0]["membership"]["groups"])
        self.assertEqual(self.index.checkpoint()["generation"], 2)

    def test_unchanged_heartbeat_does_not_increment_content_revision(self):
        self.seed()
        old = self.index.checkpoint()["revision"]
        self.index.publish([], "2026-11-10T10:01:00Z", {"1": "heartbeat"})
        self.assertEqual(self.index.checkpoint()["revision"], old)
        self.assertEqual(self.index.checkpoint()["readTime"], "2026-11-10T10:01:00Z")

    def test_incompatible_schema_rebuilds_instead_of_serving_an_unknown_layout(self):
        self.seed()
        with self.index.db() as db:
            db.execute("PRAGMA user_version=999")
        reopened = live.Index(self.env)
        self.addCleanup(reopened.close)
        with self.assertRaises(live.NotReady):
            reopened.snapshot()

    def test_identity_change_discards_other_credentials_index(self):
        self.seed()
        from test_operator_config import creds_b64
        other = live.Index(dict(self.env, GCP_SERVICE_ACCOUNT_B64=creds_b64(project_id="other")))
        self.addCleanup(other.close)
        with self.assertRaises(live.NotReady):
            other.snapshot()

    def test_warm_pages_search_scroll_schedule_and_metrics_have_zero_firestore_calls(self):
        view = self.service()
        # Inject enough indexed rows to force pagination; no Firestore request.
        changes = [("groups", "%s@g.us" % n, config.encode_fields({"name": "Market %s" % n,
                    "status": "active", "account": "default", "promos": ["cv"]})) for n in range(10000, 10025)]
        self.index.publish(changes, "2026-11-10T10:01:00Z", {})
        with patch.object(config, "fetch", side_effect=AssertionError("Firestore fetch")), \
             patch.object(config, "get_document", side_effect=AssertionError("Firestore point read")), \
             patch.object(config, "list_documents", side_effect=AssertionError("Firestore scan")):
            first = view.handle("groups", "")
            self.assertEqual(len(first["rows"]), 10)
            second = view.handle("groups", "cursor=" + first["nextCursor"])
            self.assertEqual(len(second["rows"]), 10)
            self.assertFalse(set(row["jid"] for row in first["rows"]) & set(row["jid"] for row in second["rows"]))
            search = view.handle("groups", "q=Market+10024&status=active&line=default")
            self.assertEqual([row["jid"] for row in search["rows"]], ["10024@g.us"])
            self.assertTrue(view.handle("groups", "since=" + first["revision"])["unchanged"])
            schedule = view.handle("schedule", "")
            self.assertNotIn("groups", schedule)
            self.assertTrue(view.handle("schedule", "since=" + schedule["revision"])["unchanged"])
            self.assertEqual(len(view.handle("metrics", "ids=" + JID_A)["rows"]), 1)
            view.handle("discoveries", "")
            view.handle("campaigns", "")
            self.assertIn("capSettings", view.handle("settings", ""))
            self.assertIn("lines", view.handle("lines", ""))
            captions = view.handle("captions", "slug=cv")
            self.assertIn("captions", captions)
            self.assertNotIn("groups", captions)
            self.assertNotIn("adAssets", captions)

    def test_observation_endpoints_do_not_load_full_indexed_config(self):
        view = self.service()
        with patch.object(self.index, "snapshot", side_effect=AssertionError("full membership read")):
            self.assertEqual(len(view.handle("metrics", "ids=" + JID_A)["rows"]), 1)
            self.assertEqual(view.handle("discoveries", "")["rows"], [])

    def test_stale_cursor_resets_and_filter_bound_cursor_is_refused(self):
        view = self.service()
        rows = [{"jid": str(i), "name": "Group"} for i in range(25)]
        first = view.page("groups", rows, {}, "v1", {}, {})
        self.assertTrue(view.page("groups", rows, {"cursor": first["nextCursor"]}, "v2", {}, {})["reset"])
        with self.assertRaises(ValueError):
            view.page("groups", rows, {"cursor": first["nextCursor"], "q": "other"}, "v1", {}, {})
        anchor = view.page("groups", rows, {"anchor": "12"}, "v1", {}, {})
        self.assertEqual(anchor["rows"][0]["jid"], "12")

    def test_post_write_fence_refuses_older_index_until_consistent_boundary(self):
        view = self.service()
        view.write_fence = "2026-11-10T10:01:00Z"
        with patch.object(self.index, "await_boundary", return_value=False):
            with self.assertRaises(live.NotReady):
                view.handle("groups", "")
            held = view.handle("groups", "since=confirmed-client-result")
            self.assertTrue(held["unchanged"])
            self.assertEqual(held["sync"]["state"], "awaiting-commit")
        # Local metrics remain available while config waits for the commit.
        self.assertEqual(view.handle("metrics", "ids=" + JID_A)["rows"][0]["jid"], JID_A)
        self.index.publish([], "2026-11-10T10:02:00Z", {})
        self.assertTrue(view.handle("groups", "")["rows"])

    def test_save_resumes_stream_and_waits_for_prompt_boundary_without_rebuild(self):
        view = self.service()
        self.index.cancel = Mock()
        view.begin_write()
        view.fence()
        self.index.cancel.assert_called_once()
        self.assertTrue(self.index.restart_requested)
        self.assertEqual(self.index.checkpoint()["tokens"], {"1": "resume"})
        before = self.index.counters.copy()
        # A resumed Listen confirms a newer boundary even if no documents changed.
        def confirm():
            time.sleep(.02)
            self.index.publish([], "2099-01-01T00:00:00Z", {"1": "resumed"})
        worker = threading.Thread(target=confirm)
        worker.start()
        try:
            self.assertTrue(view.handle("groups", "")["rows"])
            self.assertIsNone(view.write_fence)
            self.assertEqual(self.index.counters["rebuilds"], before["rebuilds"])
            self.assertEqual(self.index.counters["syncDocuments"], before["syncDocuments"])
        finally:
            worker.join()

    def test_telemetry_is_not_locked_behind_a_slow_commit_confirmation(self):
        view = self.service()
        view.write_fence = "2099-01-01T00:00:00Z"
        waiting, release, measured = threading.Event(), threading.Event(), threading.Event()
        def slow_boundary(*args):
            waiting.set()
            release.wait(3)
            return False
        def read_groups():
            view.handle("groups", "since=previous")
        def read_metrics():
            try:
                view.handle("metrics", "ids=" + JID_A)
                measured.set()
            except Exception:
                pass
        with patch.object(self.index, "await_boundary", side_effect=slow_boundary):
            reader = threading.Thread(target=read_groups)
            reader.start()
            self.assertTrue(waiting.wait(1))
            metric_reader = threading.Thread(target=read_metrics)
            metric_reader.start()
            try:
                self.assertTrue(measured.wait(1), "telemetry waited for Firestore confirmation")
            finally:
                release.set()
                reader.join()
                metric_reader.join()

    def test_request_validation_and_telemetry_are_bounded(self):
        view = self.service()
        for query in ("unknown=1", "q=a&q=b", "cursor=invalid", "ids=not-a-group"):
            with self.assertRaises(ValueError):
                view.handle("metrics" if query.startswith("ids") else "groups", query)


class StreamProtocolTests(LiveIndexTests):
    def test_sdk_initial_sync_resume_and_matching_existence_filter(self):
        from google.cloud.firestore_v1.types import ListenResponse
        from google.protobuf.timestamp_pb2 import Timestamp
        from google.protobuf.json_format import ParseDict
        from google.cloud.firestore_v1.types import Document
        target_ids = list(range(1, len(live.COLLECTIONS) + 1))
        events = []
        for collection, docs in self.firestore.store.items():
            if collection not in live.COLLECTIONS:
                continue
            for name, fields in docs.items():
                document = Document()
                ParseDict({"name": "projects/test/databases/(default)/documents/%s/%s" % (collection, name), "fields": fields}, document._pb)
                events.append(ListenResponse(document_change={"document": document, "target_ids": [live.COLLECTIONS.index(collection) + 1]}))
        events.append(ListenResponse(target_change={"target_change_type": 3, "target_ids": target_ids}))
        events.append(ListenResponse(filter={"target_id": 4, "count": len(self.firestore.store["groups"])}))
        events.append(ListenResponse(target_change={"target_change_type": 0, "read_time": datetime.datetime(2026, 11, 10, 10, tzinfo=datetime.timezone.utc), "resume_token": b"saved"}))
        class Responses:
            def __init__(self, values): self.values = values
            def __iter__(self): return iter(self.values)
            def cancel(self): pass
        requests = []
        client = Mock()
        def listen(*, requests: object, metadata, retry):
            self.assertEqual(metadata, (("google-cloud-resource-prefix", "projects/genaie-test/databases/(default)"),))
            self.assertIsNone(retry)
            iterator = iter(requests)
            for _ in live.COLLECTIONS:
                captured.append(next(iterator))
            return Responses(events)
        self.index.lease_until = self.clock[0] + 90
        captured = []
        client.listen.side_effect = listen
        with patch("google.cloud.firestore_v1.services.firestore.FirestoreClient", return_value=client), \
             patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=Mock()):
            live.firestore_stream(self.index)
            self.assertTrue(self.index.checkpoint()["readTime"])
            self.assertFalse(any(request.add_target.resume_token for request in captured))
            captured.clear()
            events.clear()
            live.firestore_stream(self.index)
            self.assertTrue(all(request.add_target.resume_token == b"saved" for request in captured))
        self.assertEqual(self.index.counters["rebuilds"], 1)



class LeaseTests(Harness):
    def test_many_consumers_share_one_stream_and_idle_disconnects(self):
        entered = threading.Event()
        calls = []
        def stream(index):
            calls.append(1)
            index.connected = True
            entered.set()
            while not index.stopped and index.clock() < index.lease_until:
                time.sleep(.01)
        index = live.Index(self.env, clock=lambda: self.clock[0], stream=stream)
        self.addCleanup(index.close)
        for _ in range(20):
            index.touch()
        self.assertTrue(entered.wait(1))
        self.assertEqual(len(calls), 1)
        self.clock[0] += 91
        deadline = time.time() + 1
        while index.connected and time.time() < deadline:
            time.sleep(.01)
        self.assertFalse(index.connected)
        index.touch()
        deadline = time.time() + 1
        while len(calls) < 2 and time.time() < deadline:
            time.sleep(.01)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
