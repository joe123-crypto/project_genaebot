"""Campaign retirement is irreversible to readers, resumable to workers and safe to retry."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
import campaign_deletion as deletion
import console_campaigns as campaigns
import operator_config as config
import operator_writer as writer
import promo_broadcast as broadcast
from test_console_campaigns import MemoryStore as CreationStore, ENV, payload, BODY, SHA
from test_operator_config import JID_A, JID_B


class Store(CreationStore):
    def __init__(self):
        super().__init__()
        self.transaction = None
    def begin(self):
        super().begin()
        self.transaction = "test-transaction"
    def rollback(self):
        super().rollback()
        self.transaction = None
    def commit(self, docs):
        super().commit(docs)
        self.transaction = None
    def request(self, suffix, body):
        assert suffix == ":commit"
        if self.fail_commit:
            raise config.ConfigUnavailable("transaction conflict")
        pending = copy.deepcopy(self.docs)
        for operation in body["writes"]:
            name = operation.get("delete") or operation["update"]["name"]
            collection, key = name.rsplit("/", 2)[-2:]
            if "delete" in operation:
                pending.setdefault(collection, {}).pop(key, None)
            else:
                pending.setdefault(collection, {})[key] = operation["update"]["fields"]
        self.docs = pending
        self.transaction = None
        return {}


class Gateway:
    def __init__(self, jobs=()):
        self.jobs = list(jobs)
        self.fail = False
    def __call__(self, args):
        if self.fail:
            raise TimeoutError("gateway unavailable")
        if args[0] == "list":
            return {"jobs": copy.deepcopy(self.jobs)}
        assert args[0] == "rm"
        self.jobs = [job for job in self.jobs if job["id"] != args[1]]
        return {}


class Retirement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {**ENV, "GENAIE_CAMPAIGN_DELETION": "1", "OPENCLAW_STATE_DIR": self.tmp.name,
                    "R2_ACCOUNT_ID": "a" * 32,
                    "R2_BUCKET": "pictures", "R2_ACCESS_KEY_ID": "test-access", "R2_SECRET_ACCESS_KEY": "test-secret"}
        self.store = Store()
        self.data = payload()
        campaigns.create(self.data, self.store, self.env, verify=lambda *args: None, reconcile=lambda *args: None)
        self.objects = []
        self.gateway = Gateway()
    def retire(self, slug="demo"):
        return deletion.delete(slug, "operator", self.store, self.env)
    def clean(self, slug="demo", remove_object=None):
        operation = self.store.get("campaign_deletions", slug)
        with patch.object(deletion.time, "time", return_value=operation["picturesAfter"] + 1):
            deletion.cleanup(slug, self.store, self.env, self.gateway, remove_object or (lambda key, env: self.objects.append(key)))
    def test_marker_audit_and_receipt_are_one_commit_then_all_reads_filter(self):
        count = len(self.store.commits)
        before = self.store.get("campaigns", "demo")
        self.assertEqual(self.retire()["cleanup"], "pending")
        self.assertEqual(len(self.store.commits), count + 1)
        self.assertTrue(self.store.get("campaigns", "demo")["deleted"])
        operation = self.store.get("campaign_deletions", "demo")
        audit = self.store.get("audit", operation["audit"])
        self.assertEqual(audit["before"], before)
        self.assertEqual(audit["change"], "deleted")
        membership, switches, _ = config.assemble({name: self.store.list(name) for name in config.COLLECTIONS})
        self.assertNotIn("demo", membership["promos"])
        self.assertFalse(switches["demo"])
        self.assertNotIn("demo", membership["groups"][JID_A]["promos"])
        self.assertNotIn(JID_A, config.quarantine(membership)[1].get("quarantined", {}))
    def test_a_campaign_with_no_document_cannot_be_retired(self):
        # There is no manifest behind the store any more, so a slug the store
        # does not hold is not a campaign that exists somewhere else.
        self.store.docs["campaigns"].pop("cv", None)
        with self.assertRaises(writer.WriteRefused) as caught:
            self.retire("cv")
        self.assertEqual(caught.exception.status, 404)
    def test_converted_campaign_keeps_its_job_name_and_runner_owned_for_cleanup(self):
        doc = dict(self.store.get("campaigns", "cv") or {},
                   kind="console", name="CV", jobName="cv-promo", enabled=True)
        self.store.docs["campaigns"]["cv"] = config.encode_fields(doc)
        self.retire("cv")
        receipt = self.store.get("campaign_deletions", "cv")
        runner = ["python3", str(Path(campaigns.__file__).resolve().parents[1] / "bin" / "run-console-campaign"), "cv"]
        self.assertIn("cv-promo", receipt["jobNames"])
        self.assertIn(runner, [entry["argv"] for entry in receipt["commands"]])
        self.gateway.jobs = [{"id": "converted", "name": "cv-promo", "payload": {"argv": runner}}]
        self.clean("cv")
        self.assertEqual(self.gateway.jobs, [])
        self.assertEqual(self.store.get("campaign_deletions", "cv")["cleanup"], "complete")

    def test_repeated_request_returns_receipt_and_does_not_duplicate_audit(self):
        first = self.retire(); commits = len(self.store.commits)
        self.assertEqual(first, self.retire())
        self.assertEqual(commits, len(self.store.commits))
        self.clean()
        self.assertEqual(self.retire()["cleanup"], "complete")
    def test_failed_commit_changes_nothing(self):
        before = copy.deepcopy(self.store.docs)
        self.store.fail_commit = True
        with self.assertRaises(config.ConfigUnavailable): self.retire()
        self.assertEqual(before, self.store.docs)
    def test_deletion_cleans_assignments_and_only_owned_jobs_and_pictures(self):
        self.retire()
        group = self.store.get("groups", JID_A)
        group.update(paused_promos=["demo", "cv"], weekly={"demo": ["Mon"], "cv": ["Tue"]}, note="concurrent edit")
        self.store.docs["groups"][JID_A] = config.encode_fields(group)
        command = self.store.get("campaign_deletions", "demo")["commands"][0]["argv"]
        self.gateway.jobs = [{"id": "demo", "name": "console-demo-promo", "payload": {"argv": command}}, {"id": "other", "name": "other"}]
        asset = self.store.get("assets", "demo-1")
        cached = Path(deletion.ad_assets.cache_path(asset, self.env)); cached.parent.mkdir(parents=True); cached.write_bytes(BODY)
        self.clean()
        self.assertEqual(self.objects, [self.data["images"][0]["key"]])
        self.assertFalse(cached.exists())
        self.assertFalse(cached.parent.exists())
        self.assertEqual(self.gateway.jobs, [{"id": "other", "name": "other"}])
        clean = self.store.get("groups", JID_A)
        self.assertEqual(clean["note"], "concurrent edit")
        self.assertNotIn("demo", clean["promos"])
        self.assertEqual(clean["paused_promos"], ["cv"])
        self.assertEqual(clean["weekly"], {"cv": ["Tue"]})
        self.assertIsNone(self.store.get("assets", "demo-1"))
        self.assertEqual(self.store.get("campaign_deletions", "demo")["cleanup"], "complete")
    def test_shared_picture_object_is_preserved(self):
        self.store.docs.setdefault("assets", {})["other-1"] = config.encode_fields(dict(self.store.get("assets", "demo-1"), campaign="other"))
        self.retire(); self.clean()
        self.assertEqual(self.objects, [])
        self.assertIsNotNone(self.store.get("assets", "other-1"))
    def test_unknown_command_under_known_job_name_is_not_removed(self):
        self.retire()
        self.gateway.jobs = [{"id": "foreign", "name": "console-demo-promo", "payload": {"argv": ["python3", "/unrelated.py"]}}]
        self.clean()
        self.assertEqual(len(self.gateway.jobs), 1)
        self.assertEqual(self.store.get("campaign_deletions", "demo")["cleanup"], "pending")
    def test_interrupted_object_cleanup_retries_after_index_was_removed(self):
        self.retire()
        self.clean(remove_object=lambda *args: (_ for _ in ()).throw(TimeoutError()))
        self.assertIsNone(self.store.get("assets", "demo-1"))
        self.assertEqual(self.store.get("campaign_deletions", "demo")["cleanup"], "pending")
        self.clean()
        self.assertEqual(len(self.objects), 1)
        self.assertEqual(self.store.get("campaign_deletions", "demo")["cleanup"], "complete")
    def test_gateway_failure_stays_pending_and_a_new_worker_can_resume(self):
        self.retire(); self.gateway.fail = True; self.clean()
        self.assertTrue(self.store.get("campaigns", "demo")["deleted"])
        self.assertEqual(self.store.get("campaign_deletions", "demo")["cleanup"], "pending")
        self.gateway = Gateway(); self.clean()
        self.assertEqual(self.store.get("campaign_deletions", "demo")["cleanup"], "complete")
    def test_reserved_uploads_are_captured_and_cleanup_waits_for_expiry(self):
        extra = dict(self.store.get("assets", "demo-1"), id="new-image", filename="new.png", key="ads/demo/%s.png" % ("b" * 64), sha256="b" * 64)
        self.store.docs["campaign_asset_uploads"] = {"inflight": config.encode_fields({"assets": [extra], "expiresAt": 9999999999})}
        self.retire(); self.clean()
        self.assertEqual(self.objects, [])
        self.store.docs["campaign_asset_uploads"]["inflight"]["expiresAt"] = config.encode_value(0)
        self.clean()
        self.assertEqual(set(self.objects), {extra["key"], self.data["images"][0]["key"]})
    def test_rejects_edits_assignments_and_creation_retry_for_deleted_slug(self):
        self.retire()
        raw = {name: self.store.list(name) for name in config.COLLECTIONS}
        for collection, key, change in [("campaigns", "demo", {"enabled": True}), ("groups", JID_A, {"promos": ["demo"]})]:
            with self.assertRaises(writer.WriteRefused): writer.prepare(raw, collection, key, change)
        with self.assertRaises(writer.WriteRefused):
            campaigns.create(self.data, self.store, self.env, verify=lambda *args: None)
        with self.assertRaises(writer.WriteRefused):
            writer.write_group_batch([{"id": JID_A, "fields": {"promos": ["demo"]}}], "Operator", store=self.store)
    def test_capability_requires_opt_in_live_configuration_and_r2_credentials(self):
        self.assertTrue(deletion.capability(self.env)["delete"])
        for key in deletion.R2_VARS + (deletion.FLAG,):
            self.assertFalse(deletion.capability({k: v for k, v in self.env.items() if k != key})["delete"])
    def test_stale_snapshot_is_never_used_with_deletion_protection(self):
        with patch.object(config, "fetch", side_effect=config.ConfigUnavailable("offline")), patch.object(config, "_from_snapshot") as fallback:
            with self.assertRaises(config.ConfigUnavailable): config.load(self.env, log=lambda _: None)
            fallback.assert_not_called()
    def test_live_guard_stops_the_second_group_and_fails_closed(self):
        image = Path(self.tmp.name) / "image.png"; image.write_bytes(BODY)
        def run():
            return broadcast.broadcast(text="offer", image=str(image),
                state_file=str(Path(self.tmp.name) / "state.json"), promo_name="demo-promo", send_delay=0, start_jitter=0,
                caps=None, resolve_config=lambda *args, **kwargs: {"mode": "firestore", "source": "firestore", "enabled": True, "membership": {}, "switches": {}, "doc": {"groups": ["1@g.us", "2@g.us"]}}, sleep=lambda _: None, account_status=lambda accounts: {a: True for a in accounts}, argv=[])
        with patch.dict(os.environ, dict(self.env, GENAIE_AD_ASSETS_SOURCE="repo")), patch("sys.stdout", io.StringIO()), patch.object(deletion, "live_campaign", side_effect=[{}, {"deleted": True}]), patch.object(broadcast, "send_ad", return_value=(True, "sent")) as send:
            self.assertEqual(run(), 0)
            self.assertEqual(send.call_count, 1)
        # A different state file isn't needed: the one remaining group is still pending.
        with patch.dict(os.environ, dict(self.env, GENAIE_AD_ASSETS_SOURCE="repo")), patch.object(deletion, "live_campaign", side_effect=config.ConfigUnavailable("offline")), patch.object(broadcast, "send_ad") as send:
            with self.assertRaises(config.ConfigUnavailable): run()
            send.assert_not_called()
    def test_live_group_edits_use_transaction_and_preserve_atomic_failure(self):
        before = copy.deepcopy(self.store.docs)
        with patch.object(campaigns, "Store", return_value=self.store):
            result = writer.write("campaigns", "demo", {"enabled": True}, "Operator", self.env, dry_run=True)
            self.assertEqual(result["change"], "updated")
            self.assertEqual(before, self.store.docs)
            self.store.fail_commit = True
            with self.assertRaises(config.ConfigUnavailable):
                writer.write("campaigns", "demo", {"enabled": True}, "Operator", self.env)
            self.assertEqual(before, self.store.docs)
    def test_a_retired_campaign_never_reappears_in_a_read_or_a_preview(self):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("deleted_read", str(Path(__file__).resolve().parents[1] / "bin" / "console-read"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        read = importlib.util.module_from_spec(spec); loader.exec_module(read)
        self.retire("cv"); self.retire("demo")
        raw = {name: self.store.list(name) for name in config.COLLECTIONS}
        membership, enabled, _ = config.assemble(raw)
        read._RESOLVED["config"] = {"membership": membership, "switches": enabled, "storeSwitches": enabled, "deleted": ["cv"]}
        read._CATALOGUE["value"] = {"campaigns": {}}
        import datetime
        # The read builds every row from the store now, picture URLs included,
        # so it needs the live asset origin the console API is given.
        with patch.dict(os.environ, self.env):
            slugs = [row["slug"] for row in read.campaign_records(datetime.date(2026, 11, 9))]
            self.assertEqual(sorted(set(slugs) & {"cv", "demo"}), [])
            with self.assertRaises(read.NotFound): read.preview("cv", datetime.date(2026, 11, 9))
        with patch.dict(os.environ, self.env), patch.object(config, "credentials", return_value={"project_id": "test"}), patch.object(config, "access_token", return_value="tok"), patch.object(config, "get_document", return_value={"deleted": True, "enabled": False}):
            self.assertEqual(read.campaign_edit_state("cv"), {"campaign": None})
            with self.assertRaises(read.NotFound): read.campaign_edit_state("cv", captions_only=True)
            with patch.object(config, "count_documents", return_value=0), patch.object(config, "query_documents", return_value=[("cv", {})]), patch.object(read.console_config, "cached_summary", side_effect=lambda name, fn: fn()):
                self.assertEqual(read.navigation()["campaignTotal"], 0)
    def test_r2_deletion_signs_exact_keys_and_treats_404_as_already_removed(self):
        import urllib.error
        key = self.data["images"][0]["key"]
        with patch.object(deletion.urllib.request, "urlopen", return_value=io.BytesIO()) as request:
            deletion.delete_object(key, self.env)
            sent = request.call_args.args[0]
            self.assertEqual(sent.method, "DELETE")
            self.assertTrue(sent.full_url.endswith("/pictures/" + key))
            self.assertIn("AWS4-HMAC-SHA256", sent.get_header("Authorization"))
        with patch.object(deletion.urllib.request, "urlopen", side_effect=urllib.error.HTTPError("test", 404, "missing", {}, None)):
            deletion.delete_object(key, self.env)
    def test_completed_receipts_remove_jobs_reintroduced_by_stale_installers(self):
        self.retire(); self.clean()
        command = self.store.get("campaign_deletions", "demo")["commands"][0]["argv"]
        self.gateway.jobs = [{"id": "stale", "name": "console-demo-promo", "payload": {"argv": command}}]
        self.clean()
        self.assertEqual(self.gateway.jobs, [])
    def tool(self, filename):
        import importlib.util
        spec = importlib.util.spec_from_file_location("retirement_tool", Path(__file__).resolve().parents[2] / "deploy" / filename)
        tool = importlib.util.module_from_spec(spec); spec.loader.exec_module(tool)
        return tool
    def run_upload(self, put):
        """One ``upload-ad-image.py --write`` against the fake store.

        The tool reads the campaign list and the current index from Firestore
        now: there is no cron manifest to name the campaigns and no committed
        mirror of the index to start from, so the only things stubbed are the
        credentials, the HTTP reads and the bucket itself.
        """
        from contextlib import ExitStack
        tool = self.tool("upload-ad-image.py")
        # cv as a data-defined campaign with one picture, which is what this
        # upload replaces.
        # Distinct bytes from the demo campaign's picture: keys are content
        # addressed, so identical bytes would be one object for both campaigns
        # and this would be a test about deduplication instead.
        body = BODY + b"-cv"
        picture = Path(self.tmp.name) / "cv-ad.png"
        picture.write_bytes(body)
        self.store.docs["campaigns"]["cv"] = config.encode_fields(
            {"kind": "console", "name": "CV", "enabled": True, "captions": ["hi"],
             "schedule": {"time": "11:39", "timezone": "Africa/Harare",
                          "weekdays": list(range(7))},
             "timeoutSeconds": 7200, "failureAlertTo": "", "scheduleStatus": "ready",
             "images": []})
        key = "ads/cv/%s.png" % hashlib.sha256(body).hexdigest()
        with ExitStack() as stack:
            stack.enter_context(patch.object(tool, "firestore_credentials", return_value={"project_id": "test"}))
            stack.enter_context(patch.object(tool, "token_for", return_value="test-token"))
            stack.enter_context(patch.object(tool.operator_config, "list_documents", side_effect=lambda project, collection, token, **kwargs: self.store.list(collection)))
            stack.enter_context(patch.object(tool.r2_sigv4, "settings", return_value=("https://r2.example", "pictures", "access", "secret")))
            stack.enter_context(patch.object(tool.r2_sigv4, "put_object", side_effect=put))
            stack.enter_context(patch.object(campaigns, "Store", return_value=self.store))
            stack.enter_context(patch("sys.stdout", io.StringIO()))
            stack.enter_context(patch("sys.stderr", io.StringIO()))
            result = tool.main(["upload-ad-image.py", "cv", str(picture),
                                "--no-env-file", "--write"])
        return result, key
    def test_upload_tool_reserves_keys_and_removes_receipt_after_index_commit(self):
        observed = []
        def put(*args, **kwargs):
            observed.extend(self.store.list("campaign_asset_uploads"))
        result, key = self.run_upload(put)
        self.assertEqual(result, 0)
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.store.get("assets", "cv-1")["key"], key)
        self.assertEqual(self.store.list("campaign_asset_uploads"), [])
        # The campaign's own images list is what a run attaches, so indexing a
        # picture without updating it would upload a swap that never goes out.
        self.assertEqual([image["key"] for image
                          in self.store.get("campaigns", "cv")["images"]], [key])
    def test_deletion_during_upload_captures_unindexed_key_and_refuses_index_write(self):
        result, key = self.run_upload(lambda *args, **kwargs: self.retire("cv"))
        self.assertEqual(result, 2)
        self.assertIsNone(self.store.get("assets", "cv-1"))
        self.assertIn(key, [asset["key"] for asset in self.store.get("campaign_deletions", "cv")["assets"]])
        self.clean("cv")
        self.assertEqual(self.objects, [])
        for receipt in self.store.docs["campaign_asset_uploads"]:
            self.store.docs["campaign_asset_uploads"][receipt]["expiresAt"] = config.encode_value(0)
        self.clean("cv")
        self.assertIn(key, self.objects)
    # Seeding the store from the committed file and checking the gateway against
    # the committed manifest are both gone with those files. That a recovery tool
    # cannot undo a retirement is now pinned on the tool that replaced them, in
    # deploy/test_backup_operator_data.py.
    def test_picture_cleanup_waits_for_preexisting_browser_upload_urls(self):
        self.retire()
        deletion.cleanup("demo", self.store, self.env, self.gateway, lambda key, env: self.objects.append(key))
        self.assertEqual(self.objects, [])
        self.assertIsNotNone(self.store.get("assets", "demo-1"))
        self.assertNotIn("demo", self.store.get("groups", JID_A)["promos"])
        self.assertIn("upload URLs", self.store.get("campaign_deletions", "demo")["detail"])
        self.clean()
        self.assertEqual(self.store.get("campaign_deletions", "demo")["cleanup"], "complete")
    def test_retirement_safe_single_group_write_keeps_the_scoped_read_budget(self):
        with patch.object(campaigns, "Store", return_value=self.store), patch.object(self.store, "list", wraps=self.store.list) as reads:
            writer.write("groups", JID_A, {"note": "updated"}, "Operator", self.env, dry_run=True)
        self.assertNotIn("groups", [call.args[0] for call in reads.call_args_list])
    def test_cleanup_does_not_create_or_run_a_gateway_job(self):
        self.retire(); self.clean(); self.clean()
        self.assertEqual(self.gateway.jobs, [])


if __name__ == "__main__":
    unittest.main()
