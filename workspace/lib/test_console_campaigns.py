"""Campaign creation, verified assets, scheduler recovery and real engine dry runs."""
import copy
import datetime as dt
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
import console_campaigns as cc
import operator_config as config
import operator_writer as writer
import promo_broadcast
from test_operator_config import seed_store, JID_A, JID_B, JID_C

ENV = {"GENAIE_CONFIG_SOURCE": "firestore", "GENAIE_AD_ASSETS_SOURCE": "r2", "GENAIE_AD_ASSETS_BASE_URL": "https://assets.example.test"}
BODY = b"\x89PNG\r\n\x1a\nexample-image"
SHA = hashlib.sha256(BODY).hexdigest()


def payload():
    return {"requestId": "12345678-1234-1234-1234-123456789abc", "slug": "demo", "name": "Demo Campaign", "enabled": False,
            "captions": ["Hello {bot_number}"], "schedule": {"time": "09:05", "timezone": "Africa/Harare", "weekdays": list(range(7))},
            "groups": [{"jid": JID_A}, {"jid": JID_B}],
            "images": [{"filename": "one.png", "key": "ads/demo/%s.png" % SHA, "sha256": SHA, "bytes": len(BODY), "contentType": "image/png"}]}


class MemoryStore:
    base = "projects/test/databases/(default)/documents"
    def __init__(self):
        self.docs = seed_store()
        self.commits = []
        self.in_transaction = False
        self.fail_commit = False
    def get(self, collection, key):
        fields = self.docs.get(collection, {}).get(key)
        return config.decode_fields(fields) if fields is not None else None
    def list(self, collection, where=None):
        rows = copy.deepcopy(list(self.docs.get(collection, {}).items()))
        if where is None:
            return rows
        # The two filters the code under test issues: equality, and membership
        # of a string in an array field.
        clause = where["fieldFilter"]
        name, wanted = clause["field"]["fieldPath"], config.decode_value(clause["value"])
        def keep(fields):
            value = config.decode_fields(fields).get(name)
            return wanted in (value or []) if clause["op"] == "ARRAY_CONTAINS" else value == wanted
        return [(key, fields) for key, fields in rows if keep(fields)]
    def begin(self):
        self.in_transaction = True
    def rollback(self):
        self.in_transaction = False
    def commit(self, docs):
        if self.fail_commit:
            raise config.ConfigUnavailable("transaction conflict")
        pending = copy.deepcopy(self.docs)
        for collection, key, fields, exists in docs:
            if exists is False and key in pending.get(collection, {}):
                raise config.ConfigUnavailable("already exists")
            pending.setdefault(collection, {})[key] = config.encode_fields(fields)
        self.docs = pending
        self.commits.append(copy.deepcopy(docs))
        self.in_transaction = False
    def request(self, suffix, body):
        assert suffix == ":commit"
        for write in body["writes"]:
            collection, key = write["update"]["name"].rsplit("/", 2)[-2:]
            self.docs[collection][key].update(write["update"]["fields"])
        return {}


class FakeGateway:
    def __init__(self):
        self.jobs = []
        self.calls = []
        self.lose_add_ack = False
    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "list":
            return {"jobs": copy.deepcopy(self.jobs)}
        if args[0] == "add":
            # cron add carries no failure-alert options; only cron edit does.
            if any(arg.startswith("--failure-alert") for arg in args):
                raise subprocess.CalledProcessError(1, args, stderr='OpenClaw does not recognize option "--failure-alert".')
            job = {"id": "one", "enabled": False}
            self.jobs.append(job)
        else:
            job = next(job for job in self.jobs if job["id"] == args[1])
        def flag(name):
            return args[args.index(name) + 1] if name in args else None
        for option, key in [("--name", "name")]:
            if flag(option): job[key] = flag(option)
        if "--cron" in args: job["schedule"] = {"expr": flag("--cron"), "tz": flag("--tz")}
        if "--command-argv" in args:
            job["payload"] = {"argv": json.loads(flag("--command-argv")), "cwd": flag("--command-cwd"), "timeoutSeconds": int(flag("--timeout-seconds"))}
        if "--enable" in args: job["enabled"] = True
        if "--disable" in args: job["enabled"] = False
        if "--failure-alert-to" in args: job["failureAlert"] = {"to": flag("--failure-alert-to")}
        if args[0] == "add" and self.lose_add_ack:
            self.lose_add_ack = False
            raise TimeoutError("ack lost")
        return {}


class Creation(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.data = payload()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env_patch = patch.dict(os.environ, {**ENV, "OPENCLAW_STATE_DIR": self.temp.name})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
    def create(self, **kwargs):
        return cc.create(self.data, self.store, env=ENV, verify=lambda *args: None, reconcile=lambda *args: None, **kwargs)
    def test_minimum_form_is_atomic_and_preserves_group_configuration(self):
        before = self.store.get("groups", JID_A)
        self.assertEqual(self.create()["scheduling"], "ready")
        self.assertEqual(len(self.store.commits), 1)
        campaign = self.store.get("campaigns", "demo")
        self.assertFalse(campaign["enabled"])
        self.assertEqual(campaign["timeoutSeconds"], 7200)
        self.assertEqual(campaign["tracking"], {})
        self.assertEqual(campaign["failureAlertTo"], "")
        group = self.store.get("groups", JID_A)
        self.assertEqual(group["promos"], before["promos"] + ["demo"])
        for key in before.keys() - {"promos"}:
            self.assertEqual(group[key], before[key])
        self.assertTrue(self.store.docs["audit"])
        self.assertTrue(self.store.docs["assets"])
        self.assertFalse(self.store.in_transaction)
    def test_same_operation_retries_without_writes_and_changed_payload_is_refused(self):
        self.create()
        self.create()
        self.assertEqual(len(self.store.commits), 1)
        self.data["name"] = "Changed"
        with self.assertRaises(writer.WriteRefused): self.create()
    def test_duplicate_id_never_overwrites(self):
        self.create()
        self.data["requestId"] = "abcdefab-1234-1234-1234-123456789abc"
        with self.assertRaises(writer.WriteRefused): self.create()
        self.assertEqual(len(self.store.commits), 1)
    def test_no_commit_if_a_group_is_inactive_or_commit_fails(self):
        self.data["groups"].append({"jid": JID_C})
        with self.assertRaises(writer.WriteRefused): self.create()
        self.assertNotIn("demo", self.store.docs["campaigns"])
        self.data = payload(); self.store.fail_commit = True
        with self.assertRaises(config.ConfigUnavailable): self.create()
        self.assertNotIn("demo", self.store.docs["campaigns"])
    def test_weekly_pauses_and_tracking_defaults(self):
        self.data["schedule"]["weekdays"] = [0, 2, 4]
        self.data["groups"][0].update(weekdays=[0], paused=True)
        self.create()
        self.assertEqual(self.store.get("groups", JID_A)["weekly"]["demo"], ["Mon"])
        self.assertIn("demo", self.store.get("groups", JID_A)["paused_promos"])
        self.assertEqual(self.store.get("groups", JID_B)["weekly"]["demo"], ["Mon", "Wed", "Fri"])
    def test_invalid_inputs_are_refused_before_any_write(self):
        for changes in [{"captions": [" "]}, {"images": []}, {"groups": []}, {"timeoutSeconds": True}, {"tracking": {"number": "+10000000000"}}, {"schedule": {"time": "25:00", "timezone": "Africa/Harare", "weekdays": [0]}}]:
            with self.subTest(changes=changes), self.assertRaises(writer.WriteRefused): cc.validate({**payload(), **changes})
        self.assertEqual(self.store.commits, [])
    def test_saved_pending_recovers_after_lost_gateway_ack_and_restart(self):
        gateway = FakeGateway(); gateway.lose_add_ack = True
        reconcile = lambda slug, store: cc.reconcile_one(slug, store, call=gateway)
        result = cc.create(self.data, self.store, ENV, verify=lambda *args: None, reconcile=reconcile)
        self.assertEqual(result["scheduling"], "pending")
        self.assertEqual(len(gateway.jobs), 1)
        self.assertEqual(cc.create(self.data, self.store, ENV, verify=lambda *args: None, reconcile=reconcile)["scheduling"], "ready")
        self.assertEqual(len(gateway.jobs), 1)
        edits_before = len([call for call in gateway.calls if call[0] != "list"])
        cc.reconcile_one("demo", self.store, call=gateway)
        self.assertEqual(edits_before, len([call for call in gateway.calls if call[0] != "list"]))
        self.assertFalse(gateway.jobs[0]["enabled"])
    def test_enabled_creation_sets_future_recurring_job_without_sending(self):
        self.data["enabled"] = True
        self.data["schedule"]["weekdays"] = [0, 2, 4]
        gateway = FakeGateway()
        result = cc.create(self.data, self.store, ENV, verify=lambda *args: None, reconcile=lambda slug, store: cc.reconcile_one(slug, store, gateway))
        self.assertEqual(result["scheduling"], "ready")
        self.assertTrue(gateway.jobs[0]["enabled"])
        self.assertEqual(gateway.jobs[0]["schedule"]["expr"], "5 9 * * 1,3,5")
        self.assertFalse(any("run" in call for call in gateway.calls))
    def test_on_the_hour_schedules_and_unreachable_group_counts_are_refused(self):
        on_the_hour = dict(payload(), schedule={"time": "09:00", "timezone": "Africa/Harare", "weekdays": [0]})
        with self.assertRaisesRegex(writer.WriteRefused, "not on the hour"): cc.validate(on_the_hour)
        # The ceilings the repository's CI held campaigns to while their target
        # lists were committed files: 30 groups an hour, 60 in the two-hour job.
        for timeout, fits in ((3600, 30), (5400, 45), (7200, 60)):
            self.assertIsNone(cc.spacing_problem(fits, timeout))
            self.assertIn("full spacing", cc.spacing_problem(fits + 1, timeout))
        crowded = dict(payload(), timeoutSeconds=600, groups=[{"jid": "1203630000000000%02d@g.us" % n} for n in range(6)])
        with self.assertRaisesRegex(writer.WriteRefused, "full spacing"): cc.validate(crowded)
    def test_converted_campaign_keeps_its_job_and_state_under_its_old_name(self):
        self.data["enabled"] = True
        self.create()
        self.store.docs["campaigns"]["demo"].update(config.encode_fields({"jobName": "demo-promo"}))
        self.assertEqual(cc.job_name("demo", self.store.get("campaigns", "demo")), "demo-promo")
        self.assertEqual(cc.job_name("demo", {"jobName": "../escape"}), "console-demo-promo")
        # The job the installer registered: same name, still running the script.
        gateway = FakeGateway()
        gateway.jobs.append({"id": "legacy", "name": "demo-promo", "enabled": True,
                             "schedule": {"expr": "5 9 * * *", "tz": "Africa/Harare"},
                             "payload": {"argv": ["python3", "/workspace/cron/demo-promo/post_ad.py"],
                                         "cwd": "/workspace", "timeoutSeconds": 7200}})
        cc.reconcile_one("demo", self.store, call=gateway)
        self.assertEqual([job["id"] for job in gateway.jobs], ["legacy"])
        self.assertEqual(gateway.jobs[0]["payload"]["argv"][-1], "demo")
        self.assertTrue(gateway.jobs[0]["payload"]["argv"][-2].endswith("bin/run-console-campaign"))
        self.assertTrue(gateway.jobs[0]["enabled"])
        self.assertEqual(self.store.get("campaigns", "demo")["scheduleStatus"], "ready")
        membership, enabled, _ = config.assemble({name: self.store.list(name) for name in config.COLLECTIONS})
        source = {"mode": "firestore", "source": "firestore", "enabled": True, "switches": enabled,
                  "membership": membership, "doc": promo_membership_payload(membership)}
        engine = Mock(return_value=0)
        self.assertEqual(cc.run_campaign("demo", now=dt.datetime(2026, 11, 9, 9, tzinfo=dt.timezone.utc),
                                         resolve=lambda _: source, ensure=lambda _: "one.png", broadcast=engine), 0)
        self.assertEqual(engine.call_args.kwargs["state_file"],
                         str(Path(self.temp.name) / "runtime" / "cron-state" / "demo-promo" / "state.json"))
        self.assertEqual(engine.call_args.kwargs["promo_name"], "demo-promo")
        # A console-created campaign stays where its state has always been.
        created = cc.state_directory("demo", {"kind": "console"})
        self.assertEqual(created, Path(self.temp.name) / "runtime" / "firestore" / "runtime" / "cron-state" / "console-demo-promo")
    def test_images_from_assets_is_the_enabled_rotation_and_round_trips(self):
        image = self.data["images"][0]
        entry = cc.image_entry("demo", image, 0)
        self.assertEqual(cc.images_from_assets([entry, dict(entry, filename="off.png", enabled=False)]), [image])
    def test_asset_verification_checks_magic_size_and_digest(self):
        image = self.data["images"][0]
        cc.verify_image(image, ENV, opener=lambda *args, **kwargs: io.BytesIO(BODY))
        for body in [b"not-an-image", BODY + b"extra", b"\x89PNG\r\n\x1a\nother-content"]:
            with self.assertRaises(writer.WriteRefused): cc.verify_image(image, ENV, opener=lambda *args, **kwargs: io.BytesIO(body))
    def test_real_engine_rotates_one_picture_and_never_resends_same_day(self):
        self.data["enabled"] = True
        self.data["captions"] = ["Offer A {bot_number}", "Offer B {bot_number}"]
        second = dict(self.data["images"][0], filename="two.png", sha256="b" * 64, key="ads/demo/%s.png" % ("b" * 64))
        self.data["images"].append(second)
        self.create()
        self.store.docs["campaigns"]["demo"].update(config.encode_fields({"scheduleStatus": "ready"}))
        membership, enabled, _ = config.assemble({name: self.store.list(name) for name in config.COLLECTIONS})
        source = {"mode": "firestore", "source": "firestore", "enabled": True, "switches": enabled,
                  "membership": membership, "doc": promo_membership_payload(membership)}
        paths = {}
        for name in ("one.png", "two.png"):
            paths[name] = str(Path(self.temp.name) / name)
            Path(paths[name]).write_bytes(BODY)
        def engine(**kwargs):
            return promo_broadcast.broadcast(**kwargs, caps=None, start_jitter=0, send_delay=0,
                         sleep=lambda _: None, account_status=lambda accounts: {account: True for account in accounts})
        def run(day):
            return cc.run_campaign("demo", now=dt.datetime(2026, 11, day, 9, tzinfo=dt.timezone.utc),
                resolve=lambda _: source, ensure=lambda entry: paths[entry["filename"]], broadcast=engine)
        with patch("sys.stdout", io.StringIO()), patch("promo_broadcast.send_ad", return_value=(True, "sent")) as send:
            self.assertEqual(run(9), 0)
            first_picture = send.call_args_list[0].args[2]
            self.assertEqual(send.call_count, 2)
            self.assertEqual({call.kwargs["account"] for call in send.call_args_list}, {"default", "ads"})
            self.assertEqual(run(9), 0)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(run(10), 0)
            self.assertEqual(send.call_count, 4)
            self.assertNotEqual(send.call_args_list[-1].args[2], first_picture)

    def test_schedule_normalizes_timezone_and_off_weekdays(self):
        self.create(); campaign = self.store.get("campaigns", "demo")
        campaign["schedule"] = {"time": "23:30", "timezone": "America/New_York", "weekdays": [0]}
        self.assertEqual(cc.schedule_minutes(campaign, dt.date(2026, 11, 10)), 390)
        self.assertIsNone(cc.schedule_minutes(campaign, dt.date(2026, 11, 9)))
    def test_preview_uses_the_occurrences_local_weekday_and_caption_date(self):
        self.data["enabled"] = True
        self.data["schedule"] = {"time": "23:30", "timezone": "America/New_York", "weekdays": [0]}
        self.create()
        self.store.docs["campaigns"]["demo"].update(config.encode_fields({"scheduleStatus": "ready"}))
        m, enabled, _ = config.assemble({name: self.store.list(name) for name in config.COLLECTIONS})
        source = {"mode": "firestore", "source": "firestore", "enabled": True, "storeSwitches": enabled,
                  "switches": {"demo": False}, "membership": m}
        loader = importlib.machinery.SourceFileLoader("campaign_preview_test", str(Path(__file__).resolve().parents[1] / "bin/console-read"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        bridge = importlib.util.module_from_spec(spec); loader.exec_module(bridge)
        with patch.object(bridge, "resolved", return_value=source):
            preview = bridge.preview("demo", dt.date(2026, 11, 10))
        self.assertTrue(preview["enabled"])
        self.assertTrue(all(row["sendsToday"] for row in preview["groups"]))
        self.assertEqual({row["account"] for row in preview["groups"]}, {"default", "ads"})

    def test_a_campaign_is_listed_once_from_the_store(self):
        self.data["enabled"] = True
        self.create()
        self.store.docs["campaigns"]["demo"].update(config.encode_fields({"scheduleStatus": "ready", "jobName": "demo-promo"}))
        m, enabled, _ = config.assemble({name: self.store.list(name) for name in config.COLLECTIONS})
        source = {"mode": "firestore", "source": "firestore", "enabled": True, "storeSwitches": enabled,
                  "switches": enabled, "membership": m}
        loader = importlib.machinery.SourceFileLoader("campaign_records_test", str(Path(__file__).resolve().parents[1] / "bin/console-read"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        bridge = importlib.util.module_from_spec(spec); loader.exec_module(bridge)
        # A campaign carrying a jobName -- one converted from a post script --
        # is listed from the store like any other, under the job it kept.
        with patch.object(bridge, "resolved", return_value=source):
            rows = [row for row in bridge.campaign_records(dt.date(2026, 11, 9)) if row["slug"] == "demo"]
        self.assertEqual([(row["source"], row["jobName"], row["error"]) for row in rows], [("console", "demo-promo", None)])
        self.assertEqual(rows[0]["captions"], ["Hello {bot_number}"])

    def test_runner_passes_two_line_targets_and_local_day_to_real_engine(self):
        self.data["enabled"] = True
        self.data["tracking"] = {"prefix": "DM"}
        self.create()
        self.store.docs["campaigns"]["demo"].update(config.encode_fields({"scheduleStatus": "ready"}))
        m, enabled, _ = config.assemble({name: self.store.list(name) for name in config.COLLECTIONS})
        source = {"mode": "firestore", "source": "firestore", "enabled": True, "switches": enabled, "membership": m, "doc": promo_membership_payload(m)}
        path = str(Path(self.temp.name) / "one.png")
        Path(path).write_bytes(BODY)
        output = io.StringIO()
        with patch("sys.stdout", output), patch("promo_broadcast.send_ad") as send:
            result = cc.run_campaign("demo", ["--dry-run"], now=dt.datetime(2026, 11, 9, 9, tzinfo=dt.timezone.utc), resolve=lambda slug: source, ensure=lambda entry: path)
        self.assertEqual(result, 0)
        self.assertIn("account ads", output.getvalue())
        self.assertIn("DM-", output.getvalue())
        send.assert_not_called()

    def test_runner_preserves_promo_suffix_in_campaign_switch_and_targets(self):
        import promo_membership
        for other_enabled in (None, False, True):
            with self.subTest(other_enabled=other_enabled):
                self.store = MemoryStore()
                self.data = payload()
                self.data.update(slug="summer-promo", enabled=True, groups=[{"jid": JID_A}])
                self.data["images"][0]["key"] = "ads/summer-promo/%s.png" % SHA
                self.create()
                self.store.docs["campaigns"]["summer-promo"].update(config.encode_fields({"scheduleStatus": "ready"}))
                if other_enabled is not None:
                    self.data.update(slug="summer", enabled=other_enabled, groups=[{"jid": JID_B}],
                                     requestId="abcdefab-1234-1234-1234-123456789abc")
                    self.data["images"][0]["key"] = "ads/summer/%s.png" % SHA
                    self.create()
                    self.store.docs["campaigns"]["summer"].update(config.encode_fields({"scheduleStatus": "ready"}))
                membership, enabled, _ = config.assemble({name: self.store.list(name) for name in config.COLLECTIONS})
                loaded = {"membership": membership, "enabled": enabled, "source": "firestore",
                          "report": {}, "degraded": False}
                engine = Mock(return_value=0)
                # Exercise the real resolver: a stub returning a fixed source
                # would miss its single '-promo' normalization step.
                with patch.object(config, "load", return_value=loaded):
                    result = cc.run_campaign("summer-promo", now=dt.datetime(2026, 11, 9, 9, tzinfo=dt.timezone.utc),
                                             broadcast=engine, ensure=lambda _: "one.png")
                    self.assertEqual(result, 0)
                    engine.assert_called_once()
                    source = engine.call_args.kwargs["resolve_config"]()
                    self.assertTrue(source["enabled"])
                    self.assertEqual(source["doc"], promo_membership.targets_payload(membership, "summer-promo"))
                    self.assertNotEqual(source["doc"], promo_membership.targets_payload(membership, "summer"))
                    self.assertEqual(engine.call_args.kwargs["promo_name"], "summer-promo-promo")
                    enabled["summer-promo"] = False
                    if other_enabled is not None:
                        enabled["summer"] = True
                    engine.reset_mock()
                    self.assertEqual(cc.run_campaign("summer-promo", broadcast=engine, ensure=lambda _: "one.png"), 0)
                    engine.assert_not_called()


def promo_membership_payload(m):
    import promo_membership
    return promo_membership.targets_payload(m, "demo")


if __name__ == "__main__":
    unittest.main()
