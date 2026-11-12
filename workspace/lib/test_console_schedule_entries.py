import copy
import datetime as dt
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import console_schedule_entries as entries
import console_schedule as schedule
import operator_config as config
import operator_writer as writer
import promo_broadcast
from test_console_schedule import Schedule, DAY, JOB, ALERT
from test_console_campaigns import FakeGateway


class EntryGateway(FakeGateway):
    def __call__(self, args):
        if args[0] == "rm":
            self.calls.append(args)
            self.jobs = [job for job in self.jobs if job["id"] != args[1]]
            return {}
        before = len(self.jobs)
        result = super().__call__(args)
        if args[0] == "add":
            self.jobs[-1]["id"] = "entry-%d" % before
        return result


class Entries(unittest.TestCase):
    def setUp(self):
        Schedule.setUp(self)
        self.gateway = EntryGateway()
        self.slug = "borehole-drilling"

    def body(self, entry_id="repeat-1", operation="request-1", **overrides):
        doc = self.store.get("campaigns", self.slug)
        change = {"slug": self.slug, "entryId": entry_id, "expectedRevision": entries.revision(doc),
                  "create": entry_id not in entries.entries(doc), "time": "12:15", "paused": False, "skipped": False, "deleted": False}
        change.update(overrides)
        return {"operationId": operation, "date": str(DAY), "timezone": "Africa/Harare", "actor": "Operator", "changes": [change]}

    def test_daily_repeat_receipt_is_atomic_and_replay_does_not_duplicate(self):
        body = self.body()
        result = entries.save(body, self.store, self.env)
        self.assertEqual(result, entries.save(body, self.store, self.env))
        self.assertEqual(len(self.store.commits), 1)
        self.assertEqual(len(self.store.get("campaigns", self.slug)["scheduleEntries"]), 2)
        self.assertEqual(result["entries"][0]["recurrence"]["weekdays"], list(range(7)))
        with self.assertRaises(writer.WriteRefused):
            entries.save({**body, "changes": [{**body["changes"][0], "time": "13:00"}]}, self.store, self.env)
        self.assertEqual(len(self.store.docs["audit"]), 1)

    def test_conflicts_and_failed_commit_leave_all_entries_unchanged(self):
        body = self.body()
        body["changes"].append({**body["changes"][0], "entryId": "repeat-2", "expectedRevision": "b" * 64})
        with self.assertRaises(writer.WriteRefused):
            entries.save(body, self.store, self.env)
        self.assertNotIn("scheduleEntries", self.store.get("campaigns", self.slug))
        self.store.fail_commit = True
        with self.assertRaises(config.ConfigUnavailable):
            entries.save(self.body(), self.store, self.env)
        self.assertNotIn("scheduleEntries", self.store.get("campaigns", self.slug))

    def test_pause_skip_delete_are_independent_and_reconcile_never_runs_now(self):
        entries.save(self.body(), self.store, self.env)
        schedule.reconcile_one(self.slug, self.store, self.env, self.gateway)
        self.assertEqual(len(self.gateway.jobs), 2)
        entries.save(self.body(operation="pause", paused=True), self.store, self.env)
        schedule.reconcile_one(self.slug, self.store, self.env, self.gateway)
        self.assertTrue(next(job for job in self.gateway.jobs if job["name"] == JOB)["enabled"])
        self.assertFalse(next(job for job in self.gateway.jobs if job["name"].startswith("console-entry"))["enabled"])
        entries.save(self.body(operation="skip", skipped=True), self.store, self.env)
        doc = self.store.get("campaigns", self.slug)
        self.assertFalse(entries.allowed(doc, "repeat-1", DAY))
        self.assertTrue(entries.allowed(doc, "primary", DAY))
        self.assertTrue(entries.allowed(doc, "repeat-1", DAY + dt.timedelta(days=1)))
        entries.save(self.body(operation="delete", deleted=True), self.store, self.env)
        schedule.reconcile_one(self.slug, self.store, self.env, self.gateway)
        entries.apply_jobs(self.slug, self.store.get("campaigns", self.slug), self.gateway)
        self.assertEqual(len(self.gateway.jobs), 1)
        self.assertTrue(self.store.get("campaigns", self.slug)["scheduleEntries"]["repeat-1"]["deleted"])
        self.assertFalse(any(call[0] == "run" for call in self.gateway.calls))

    def test_created_entry_job_takes_its_failure_alert_from_an_edit(self):
        entries.save(self.body(), self.store, self.env)
        schedule.reconcile_one(self.slug, self.store, self.env, self.gateway)
        self.assertFalse([call for call in self.gateway.calls
                          if call[0] == "add" and any(flag.startswith("--failure-alert") for flag in call)])
        job = next(job for job in self.gateway.jobs if job["name"].startswith("console-entry"))
        self.assertEqual(job["failureAlert"]["to"], ALERT)
        self.assertTrue(job["enabled"])
        entry = self.store.get("campaigns", self.slug)["scheduleEntries"]["repeat-1"]
        self.assertEqual(entry["confirmed"]["cron"], "15 12 * * *")

    def test_an_entry_may_not_be_created_or_moved_onto_the_hour(self):
        with self.assertRaisesRegex(writer.WriteRefused, "not on the hour"):
            entries.save(self.body(time="06:00"), self.store, self.env)
        self.assertNotIn("scheduleEntries", self.store.get("campaigns", self.slug))
        entries.save(self.body(), self.store, self.env)
        with self.assertRaisesRegex(writer.WriteRefused, "not on the hour"):
            entries.save(self.body(operation="move", time="06:00"), self.store, self.env)
        self.assertEqual(self.store.get("campaigns", self.slug)["scheduleEntries"]["repeat-1"]["cron"], "15 12 * * *")

    def test_a_switched_off_campaign_saves_and_is_still_revision_checked(self):
        stored = self.store.get("campaigns", self.slug)
        self.store.docs["campaigns"][self.slug] = config.encode_fields({**stored, "enabled": False})
        # The reader's document comes from assembled membership, which lifts the
        # switch out of the definition; the writer reads the stored document. The
        # precondition has to mean the same thing on both sides.
        self.assertEqual(entries.revision(stored), entries.revision({**stored, "enabled": False}))
        before = entries.revision(self.store.get("campaigns", self.slug))
        result = entries.save(self.body(), self.store, self.env)
        self.assertIs(result["entries"][0]["campaignEnabled"], False)
        self.assertIn("repeat-1", self.store.get("campaigns", self.slug)["scheduleEntries"])
        # The second write is gated as usual: the stale revision is refused and
        # the current one goes through.
        stale = self.body(operation="second", time="12:45")
        stale["changes"][0]["expectedRevision"] = before
        with self.assertRaisesRegex(writer.WriteRefused, "schedule changed"):
            entries.save(stale, self.store, self.env)
        entries.save(self.body(operation="second", time="12:45"), self.store, self.env)
        self.assertEqual(self.store.get("campaigns", self.slug)["scheduleEntries"]["repeat-1"]["cron"], "45 12 * * *")

    def test_runner_checks_current_state_and_occurrence_day(self):
        entries.save(self.body(), self.store, self.env)
        execute = unittest.mock.Mock(return_value=0)
        read = lambda slug: self.store.get("campaigns", slug)
        now = dt.datetime(2026, 11, 10, 11, tzinfo=dt.timezone.utc)
        entries.run(self.slug, "repeat-1", now=now, read=read, execute=execute)
        self.assertEqual(execute.call_args.kwargs["env"]["GENAIE_SCHEDULE_ENTRY"], "repeat-1")
        self.assertEqual(execute.call_args.kwargs["env"]["GENAIE_SCHEDULE_DAY"], str(DAY))
        entries.save(self.body(operation="skip", skipped=True), self.store, self.env)
        entries.run(self.slug, "repeat-1", now=now, read=read, execute=execute)
        self.assertEqual(execute.call_count, 1)

    def test_real_broadcast_deduplicates_each_entry_separately(self):
        entries.save(self.body(), self.store, self.env)
        image = Path(self.temp.name) / "image.png"
        image.write_bytes(b"picture")
        target = "12000000000@g.us"
        doc = self.store.get("campaigns", self.slug)
        source = {"mode": "firestore", "enabled": True, "source": "firestore", "membership": {"promos": {self.slug: doc}, "groups": {}},
                  "switches": {self.slug: True}, "doc": {"groups": [target]}, "storeSwitches": {self.slug: True}, "note": None}
        def send(entry_id):
            with patch.dict(os.environ, {"GENAIE_SCHEDULE_ENTRY": entry_id, "GENAIE_SCHEDULE_DAY": str(DAY)}):
                return promo_broadcast.broadcast(text="hello", image=str(image),
                    state_file=str(Path(self.temp.name) / "state.json"), promo_name=self.slug + "-promo", argv=[], run_day=DAY,
                    resolve_config=lambda *args, **kwargs: source, caps=None, start_jitter=0, send_delay=0,
                    account_status=lambda accounts: {account: True for account in accounts})
        with patch("campaign_deletion.live_campaign", return_value=doc), patch("promo_broadcast.send_ad", return_value=(True, "sent")) as send_ad:
            send("primary")
            send("primary")
            send("repeat-1")
            send("repeat-1")
        self.assertEqual(send_ad.call_count, 2)


if __name__ == "__main__":
    unittest.main()
