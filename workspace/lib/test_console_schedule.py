"""Schedule persistence, timezone conversion, conflict and gateway recovery tests.

Every campaign is data-defined now, so a schedule lives on the campaign's own
document and there is no manifest declaration beside it. The fixture is a
converted campaign -- ``kind: "console"`` carrying the ``jobName`` its script
job always had -- because that is the shape the ten repository campaigns took,
and the one where the job the reconciler edits is not the one its slug implies.
"""
import copy
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_console_campaigns import MemoryStore, FakeGateway
import console_schedule as schedule
import console_campaigns as campaigns
import operator_config as config
import operator_writer as writer

DAY = dt.date(2026, 11, 10)
JOB = "borehole-drilling-promo"
# Where a data-defined campaign's run actually lives, which is what its gateway
# job has to point at for reconciliation to accept the job as already correct.
RUNNER = str(Path(campaigns.__file__).resolve().parents[1] / "bin" / "run-console-campaign")
CWD = str(Path(RUNNER).parents[1])
ALERT = "+263123456789"


def campaign(time="07:15", *, job=JOB, weekdays=None, enabled=True):
    doc = {"kind": "console", "name": "Borehole Drilling", "enabled": enabled,
           "captions": ["hello"], "rank": 10, "timeoutSeconds": 7200,
           "failureAlertTo": ALERT, "scheduleStatus": "ready",
           "schedule": {"time": time, "timezone": "Africa/Harare",
                        "weekdays": list(range(7)) if weekdays is None else weekdays}}
    if job:
        doc["jobName"] = job
    return doc


def converted_job(cron="15 7 * * *", *, name=JOB, slug="borehole-drilling"):
    return {"id": "one", "name": name, "enabled": True,
            "schedule": {"expr": cron, "tz": "Africa/Harare"},
            "payload": {"argv": ["python3", RUNNER, slug], "cwd": CWD, "timeoutSeconds": 7200},
            "failureAlert": {"to": ALERT}}


class ScheduleStore(MemoryStore):
    def list(self, collection, where=None):
        rows = super().list(collection)
        if where is None:
            return rows
        condition = where["fieldFilter"]
        def matches(fields):
            value = config.decode_fields(fields)
            for key in condition["field"]["fieldPath"].split("."):
                value = value.get(key, {}) if isinstance(value, dict) else None
            expected = config.decode_value(condition["value"])
            return value == expected
        return [(slug, fields) for slug, fields in rows if matches(fields)]


class Schedule(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = {"GENAIE_CONFIG_SOURCE": "firestore", "OPENCLAW_STATE_DIR": self.temp.name}
        self.patch = patch.dict(os.environ, self.env)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.store = ScheduleStore()
        self.doc = campaign()
        self.store.docs["campaigns"]["borehole-drilling"] = config.encode_fields(self.doc)
        self.gateway = FakeGateway()
        self.gateway.jobs = [converted_job()]

    def request(self, time="07:10", slug="borehole-drilling"):
        doc = self.store.get("campaigns", slug)
        return {"date": DAY.isoformat(), "timezone": "Africa/Harare", "actor": "Operator", "changes": [
            {"slug": slug, "time": time, "expectedRevision": schedule.revision(doc)}]}

    def save(self, body=None):
        return schedule.save(body or self.request(), self.store, self.env)

    def test_drag_time_is_durable_audited_and_confirmed_only_after_readback(self):
        body = self.request()
        result = self.save(body)
        self.assertEqual(result["campaigns"][0]["minutes"], 430)
        self.assertEqual(result["campaigns"][0]["scheduleEdit"]["status"], "pending")
        self.assertEqual(result["campaigns"][0]["scheduleEdit"]["confirmedMinutes"], 435)
        saved = self.store.get("campaigns", "borehole-drilling")
        self.assertEqual(saved["captions"], ["hello"])
        # The time is the campaign's own field now; nothing shadows it.
        self.assertEqual(saved["schedule"]["time"], "07:10")
        self.assertNotIn("scheduleOverride", saved)
        self.assertEqual(len(self.store.docs["audit"]), 1)
        # A repeated HTTP request, including the old revision, is a no-op.
        self.save(body)
        self.assertEqual(len(self.store.docs["audit"]), 1)
        before = copy.deepcopy(self.gateway.jobs[0])
        schedule.reconcile_one("borehole-drilling", self.store, self.env, self.gateway)
        self.assertEqual(self.gateway.jobs[0]["schedule"]["expr"], "10 7 * * *")
        for key in ["enabled", "payload", "failureAlert"]:
            self.assertEqual(self.gateway.jobs[0][key], before[key])
        self.assertFalse(any(args[0] in ("run", "add", "rm") for args in self.gateway.calls))
        edit = schedule.describe(self.store.get("campaigns", "borehole-drilling"), DAY)
        self.assertEqual((edit["status"], edit["confirmedMinutes"]), ("ready", 430))

    def test_multi_row_acceptance_is_atomic_and_preserves_unedited_fields(self):
        doc = {**campaign("10:00", job=None, weekdays=[0, 1, 4]), "name": "Demo"}
        self.store.docs["campaigns"]["demo"] = config.encode_fields(doc)
        body = self.request()
        body["changes"].append(self.request("10:05", "demo")["changes"][0])
        self.save(body)
        self.assertEqual(len(self.store.commits), 1)
        self.assertEqual(len(self.store.commits[0]), 4)
        saved = self.store.get("campaigns", "demo")
        self.assertEqual(saved["schedule"], {**doc["schedule"], "time": "10:05"})
        self.assertEqual(saved["scheduleStatus"], "ready")
        self.assertEqual(saved["enabled"], doc["enabled"])

    def test_stale_second_row_refuses_the_whole_batch(self):
        self.store.docs["campaigns"]["other"] = config.encode_fields(campaign(job="other-promo"))
        body = self.request()
        body["changes"].append({"slug": "other", "time": "07:20", "expectedRevision": "f" * 64})
        with self.assertRaises(writer.WriteRefused) as raised:
            self.save(body)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.store.commits, [])
        self.assertEqual(self.store.get("campaigns", "borehole-drilling")["schedule"]["time"], "07:15")
        self.assertFalse(self.store.in_transaction)

    def test_commit_conflict_keeps_all_times_unchanged(self):
        self.store.fail_commit = True
        with self.assertRaises(config.ConfigUnavailable):
            self.save()
        self.assertEqual(self.store.get("campaigns", "borehole-drilling"), self.doc)
        self.assertFalse(self.store.in_transaction)

    def test_gateway_outage_and_lost_edit_ack_recover_after_restart(self):
        self.save()
        with self.assertRaises(TimeoutError):
            schedule.reconcile_one("borehole-drilling", self.store, self.env, lambda args: (_ for _ in ()).throw(TimeoutError()))
        self.assertTrue(self.store.get("campaigns", "borehole-drilling")["scheduleEdit"]["pending"])
        def lose_ack(args):
            result = self.gateway(args)
            if args[0] == "edit":
                raise TimeoutError("lost ack")
            return result
        with self.assertRaises(TimeoutError):
            schedule.reconcile_one("borehole-drilling", self.store, self.env, lose_ack)
        schedule.reconcile_one("borehole-drilling", self.store, self.env, self.gateway)
        self.assertFalse(self.store.get("campaigns", "borehole-drilling")["scheduleEdit"]["pending"])

    def test_worker_scans_only_pending_edits_and_retries_them(self):
        self.save()
        unavailable = lambda args: (_ for _ in ()).throw(TimeoutError())
        self.assertEqual(schedule.reconcile_all(self.store, self.env, unavailable), 1)
        self.assertEqual(schedule.reconcile_all(self.store, self.env, self.gateway), 0)
        before = len(self.gateway.calls)
        self.assertEqual(schedule.reconcile_all(self.store, self.env, self.gateway), 0)
        self.assertEqual(len(self.gateway.calls), before)

    def test_console_created_campaign_uses_the_existing_verified_job(self):
        doc = {**campaign("10:00", job=None, weekdays=[0, 1, 4]),
               "name": "Demo", "enabled": False, "failureAlertTo": ""}
        self.store.docs["campaigns"]["demo"] = config.encode_fields(doc)
        self.save(self.request("10:05", "demo"))
        gateway = FakeGateway()
        # The existing creation reconciler knows the console runner and settings.
        schedule.reconcile_one("demo", self.store, self.env, gateway)
        self.assertEqual(len(gateway.jobs), 1)
        self.assertEqual(gateway.jobs[0]["name"], "console-demo-promo")
        self.assertEqual(gateway.jobs[0]["schedule"]["expr"], "5 10 * * 1,2,5")
        self.assertFalse(gateway.jobs[0]["enabled"])
        self.assertFalse(self.store.get("campaigns", "demo")["scheduleEdit"]["pending"])
        self.assertFalse(any(args[0] == "run" for args in gateway.calls))

    def test_converted_campaign_repoints_its_preserved_job_rather_than_adding_one(self):
        # What the gateway holds the moment before conversion: the job name the
        # campaign keeps, still running the repository script that is now gone.
        self.gateway.jobs = [{**converted_job(), "payload": {"argv": ["python3", "/w/cron/borehole-drilling-promo/post_ad.py"]}}]
        self.save()
        schedule.reconcile_one("borehole-drilling", self.store, self.env, self.gateway)
        self.assertEqual(len(self.gateway.jobs), 1)
        self.assertEqual(self.gateway.jobs[0]["name"], JOB)
        self.assertEqual(self.gateway.jobs[0]["payload"]["argv"], ["python3", RUNNER, "borehole-drilling"])
        self.assertEqual(self.gateway.jobs[0]["schedule"]["expr"], "10 7 * * *")
        self.assertFalse(any(args[0] == "add" for args in self.gateway.calls))
        self.assertFalse(self.store.get("campaigns", "borehole-drilling")["scheduleEdit"]["pending"])

    def test_on_the_hour_edits_keep_the_bots_existing_schedule_rule(self):
        with self.assertRaises(writer.WriteRefused):
            self.save(self.request("07:00"))
        self.assertEqual(self.store.commits, [])

    def test_local_bridge_requires_the_write_key_before_any_live_access(self):
        request = self.request()
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, "-I", str(root / "bin" / "console-schedule")],
                                input=json.dumps(request), text=True, capture_output=True,
                                env={"PATH": os.environ["PATH"], **self.env}, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertEqual(json.loads(result.stdout)["status"], 503)
        self.assertEqual(self.store.commits, [])

    def test_unverified_gateway_result_never_marks_confirmed(self):
        self.save()
        def ignores_edit(args):
            return self.gateway(args) if args[0] == "list" else {}
        with self.assertRaises(writer.WriteRefused):
            schedule.reconcile_one("borehole-drilling", self.store, self.env, ignores_edit)
        self.assertEqual(schedule.describe(self.store.get("campaigns", "borehole-drilling"), DAY)["confirmedMinutes"], 435)

    def test_duplicate_jobs_are_refused_while_a_missing_one_is_created(self):
        # Reconciliation owns the job its campaign names, so a job that has gone
        # missing is simply created again. Two jobs under one name is the case it
        # must not resolve by itself: either could be the one that posts.
        self.save()
        self.gateway.jobs = self.gateway.jobs * 2
        with self.assertRaises(writer.WriteRefused):
            schedule.reconcile_one("borehole-drilling", self.store, self.env, self.gateway)
        self.assertFalse(any(args[0] in ("edit", "add") for args in self.gateway.calls))
        self.gateway.jobs = []
        schedule.reconcile_one("borehole-drilling", self.store, self.env, self.gateway)
        self.assertEqual([job["name"] for job in self.gateway.jobs], [JOB])
        self.assertEqual(self.gateway.jobs[0]["schedule"]["expr"], "10 7 * * *")

    def test_deleted_campaign_is_not_restored_by_pending_reconciliation(self):
        self.save()
        self.store.docs["campaigns"]["borehole-drilling"] = config.encode_fields({"deleted": True, "enabled": False})
        schedule.reconcile_one("borehole-drilling", self.store, self.env, self.gateway)
        self.assertEqual(self.gateway.calls, [])
        with self.assertRaises(writer.WriteRefused):
            self.save(self.request())

    def test_timezone_conversion_preserves_weekdays_and_rejects_invalid_day_shift(self):
        value = {"cron": "30 23 * * 1", "timezone": "America/New_York"}
        self.assertEqual(schedule.occurrence(value, DAY), 390)
        moved = schedule.move(value, "06:35", DAY)
        self.assertEqual(moved, {"cron": "35 23 * * 1", "timezone": "America/New_York"})
        with self.assertRaises(writer.WriteRefused):
            schedule.move(value, "12:00", DAY)
        self.assertIsNone(schedule.occurrence(value, DAY - dt.timedelta(days=1)))
        # Spring-forward native 02:30 does not exist.
        self.assertIsNone(schedule.occurrence({"cron": "30 2 * * *", "timezone": "America/New_York"}, dt.date(2026, 3, 8)))

    def test_a_schedule_without_one_editable_start_time_is_reported_uneditable(self):
        # A stored schedule can only express one daily or weekly time, so the
        # uneditable case is a document that expresses none -- no weekdays at
        # all. The console must say so rather than offer a drag that cannot save.
        broken = campaign(weekdays=[])
        described = schedule.describe(broken, DAY)
        self.assertFalse(described["editable"])
        self.assertIsNone(described["confirmedMinutes"])
        # And the mover refuses any cron it cannot rewrite as one start time.
        for cron in ["*/5 7 * * *", "15 7 1 * *", "15 7 * * 1-5"]:
            with self.assertRaises(writer.WriteRefused):
                schedule.move({"cron": cron, "timezone": "Africa/Harare"}, "07:10", DAY)

    def test_validation_refuses_without_writes(self):
        for patch_data in [{"changes": []}, {"date": "2026-02-30"}, {"timezone": "UTC"}, {"reconcile": True}, {"actor": 1}, {"changes": [{"slug": "../x"}]}]:
            with self.subTest(patch_data=patch_data), self.assertRaises(writer.WriteRefused):
                self.save({**self.request(), **patch_data})
        body = self.request(); body["changes"][0]["time"] = "25:10"
        with self.assertRaises(writer.WriteRefused): self.save(body)
        self.assertEqual(self.store.commits, [])


if __name__ == "__main__":
    unittest.main()
