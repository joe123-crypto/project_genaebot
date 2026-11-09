"""Real writer/reader contract, exact rolling windows and honest coverage."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import group_metrics as metrics

ROOT = Path(__file__).resolve().parents[2]
NOW = 1794218400000  # 2026-11-09T10:00:00Z


class GroupMetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {"OPENCLAW_STATE_DIR": self.temp.name}
        self.membership = {"defaults": {"accounts": {"ads": {"number": "+100"}}}, "groups": {}}
        self.payload = {"groups": [{"jid": "1@g.us", "cap": 3, "note": "operator", "status": "on-hold"}],
                        "discoveredGroups": [{"jid": "2@g.us"}]}

    def initialize(self):
        helper = (ROOT / "deploy/railway/whatsapp-group-activity.mjs").as_uri()
        source = '''import {EventEmitter} from 'node:events';
import {createWhatsAppGroupActivity} from %s;
const a=createWhatsAppGroupActivity({root:process.argv[1],accountId:'ads',now:()=>%s});
const ev=new EventEmitter();a.start({ev,user:{id:'100@s.whatsapp.net'}});
a.observeGroups({'1@g.us':{},'2@g.us':{}});
ev.emit('messages.upsert',{type:'notify',messages:[{key:{remoteJid:'1@g.us',id:'live',participant:'300@s.whatsapp.net'},
messageTimestamp:%s,message:{conversation:'PRIVATE CONTENT'}}]});a.stop();''' % (json.dumps(helper), NOW, NOW / 1000)
        result = subprocess.run(["node", "--input-type=module", "-e", source, self.temp.name],
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        db = sqlite3.connect(self.root / "runtime/whatsapp-activity.sqlite")
        self.addCleanup(db.close)
        return db

    def read(self, now=NOW):
        payload = copy.deepcopy(self.payload)
        membership = copy.deepcopy(self.membership)
        metrics.attach(payload, membership, self.env, now)
        self.assertEqual(membership, self.membership)
        for before, after in zip(self.payload["groups"], payload["groups"]):
            self.assertEqual(before, {key: value for key, value in after.items() if key != "metrics"})
        return payload["groups"][0]["metrics"], payload["discoveredGroups"][0]["metrics"]

    def inventory(self, account, count=42, at=NOW - 3600000):
        directory = self.root / "runtime/whatsapp-groups"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = metrics.iso(at)
        doc = {"version": 1, "accountId": account, "lastSuccessAt": stamp,
               "groups": {"1@g.us": {"name": "Group", "firstSeenAt": stamp, "lastSeenAt": stamp,
                                      "memberCount": count, "memberCountObservedAt": stamp}}}
        (directory / (account + ".json")).write_text(json.dumps(doc))

    def test_missing_telemetry_is_unavailable_and_does_not_create_files(self):
        known, discovered = self.read()
        self.assertEqual(known["coverage"], "unavailable")
        self.assertIsNone(known["dailyMessageCount"])
        self.assertEqual(known, discovered)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_actual_node_writer_and_python_reader_distinguish_zero_from_missing(self):
        self.initialize()
        known, discovered = self.read()
        self.assertEqual((known["dailyMessageCount"], discovered["dailyMessageCount"]), (1, 0))
        self.assertEqual(known["coverage"], "partial")
        self.assertEqual(known["asOf"], metrics.iso(NOW))
        self.assertNotIn("PRIVATE", json.dumps(self.read()))

    def test_bridge_reads_never_change_or_create_telemetry_files(self):
        db = self.initialize()
        db.close()
        self.inventory("ads", 42)
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.read()
        after = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_rolling_boundary_and_configured_bot_numbers_are_excluded(self):
        db = self.initialize()
        for id_, stamp, sender in [("boundary", NOW - metrics.WINDOW_MS, None),
                                    ("inside", NOW - metrics.WINDOW_MS + 1, None),
                                    ("expired", NOW - metrics.WINDOW_MS - 1, None),
                                    ("future", NOW + 1, None),
                                    ("own", NOW, hashlib.sha256(b"100@s.whatsapp.net").hexdigest())]:
            db.execute("INSERT INTO events VALUES ('1@g.us', ?, ?, ?, NULL)", (id_, stamp, sender))
        db.commit()
        self.assertEqual(self.read()[0]["dailyMessageCount"], 2)
        self.assertEqual(self.read(NOW + metrics.WINDOW_MS)[0]["dailyMessageCount"], 1)  # former future event

    def test_coverage_unions_accounts_but_never_bridges_gaps_or_stale_heartbeats(self):
        db = self.initialize()
        db.execute("DELETE FROM coverage")
        start = NOW - metrics.WINDOW_MS
        for run, first, last, live in [("ads", start, NOW - 10000, 0),
                                        ("default", NOW - 20000, NOW - 1000, 1)]:
            db.execute("INSERT INTO coverage VALUES (?, '1@g.us', ?, ?, ?)", (run, first, last, live))
        db.commit()
        self.assertEqual(self.read()[0]["coverage"], "complete")
        self.assertEqual(self.read(NOW + 100000)[0]["coverage"], "partial")
        self.env["GENAIE_GROUP_ACTIVITY"] = "0"
        self.assertEqual(self.read()[0]["coverage"], "partial")
        self.env.pop("GENAIE_GROUP_ACTIVITY")
        db.execute("UPDATE coverage SET started_at = ? WHERE run_id = 'default'", (NOW - 9999,))
        db.commit()
        self.assertEqual(self.read()[0]["coverage"], "partial")  # even a 1ms gap

    def test_member_counts_use_freshest_observation_and_retain_stale_data(self):
        self.inventory("ads", 42)
        self.inventory("default", 50, NOW - 300000)
        known = self.read()[0]
        self.assertEqual(known["memberCount"], 50)
        self.assertEqual(known["memberCountObservedAt"], metrics.iso(NOW - 300000))
        self.assertEqual(self.read(NOW + 3 * 3600000)[0]["memberCount"], 50)

    def test_invalid_members_and_broken_activity_storage_never_break_other_observations(self):
        self.inventory("ads", 42)
        self.inventory("default", True, NOW - 1000)
        (self.root / "runtime/whatsapp-activity.sqlite").write_text("broken PRIVATE")
        known = self.read()[0]
        self.assertEqual(known["memberCount"], 42)
        self.assertEqual(known["coverage"], "unavailable")
        self.assertNotIn("PRIVATE", json.dumps(known))


if __name__ == "__main__":
    unittest.main()
