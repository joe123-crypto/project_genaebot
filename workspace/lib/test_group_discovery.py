"""Inventory is review data, never an implicit campaign or membership change."""
import copy
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import group_discovery as discovery

ROOT = Path(__file__).resolve().parents[2]
STAMP = "2026-11-08T06:00:00.000Z"
LATER = "2026-11-08T07:00:00.000Z"
NOW = discovery.timestamp(LATER)


class GroupDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "runtime/whatsapp-groups"
        self.directory.mkdir(parents=True)
        self.env = {"OPENCLAW_STATE_DIR": self.temp.name}
        self.m = {"defaults": {"accounts": {"ads": {}, "default": {}}}, "groups": {}}

    def save(self, account="ads", groups=None, **fields):
        doc = {"version": 1, "accountId": account, "lastAttemptAt": STAMP,
               "lastSuccessAt": STAMP, "lastError": None,
               "groups": groups if groups is not None else {"1@g.us": {
                   "name": "Discovered", "firstSeenAt": STAMP, "lastSeenAt": STAMP}}}
        doc.update(fields)
        (self.directory / (account + ".json")).write_text(json.dumps(doc))

    def read(self, **kwargs):
        return discovery.read(self.m, env=self.env, now=kwargs.get("now", NOW))

    def test_missing_inventory_reports_waiting_without_creating_anything(self):
        result = self.read()
        self.assertEqual(result["discoveredGroups"], [])
        self.assertEqual([a["status"] for a in result["groupDiscovery"]["accounts"]], ["waiting", "waiting"])
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_duplicates_aggregate_accounts_and_use_latest_nonempty_name(self):
        self.save()
        self.save("default", {"1@g.us": {"name": "Renamed", "firstSeenAt": LATER, "lastSeenAt": LATER}})
        before = copy.deepcopy(self.m)
        row, = self.read()["discoveredGroups"]
        self.assertEqual(row["name"], "Renamed")
        self.assertEqual([a["accountId"] for a in row["accounts"]], ["ads", "default"])
        self.assertEqual(row["firstSeenAt"], STAMP)
        self.assertEqual(row["lastSeenAt"], LATER)
        self.assertEqual(self.m, before)
        self.assertNotIn("promos", row)

    def test_every_configured_status_is_excluded_without_overwriting_operator_choices(self):
        self.save(groups={str(i) + "@g.us": {"name": "WhatsApp", "firstSeenAt": STAMP, "lastSeenAt": STAMP}
                          for i in range(5)})
        for i, status in enumerate(["active", "on-hold", "admin-only", "not-a-member"]):
            self.m["groups"][str(i) + "@g.us"] = {"status": status, "name": "Operator name", "promos": []}
        before = copy.deepcopy(self.m)
        self.assertEqual([g["jid"] for g in self.read()["discoveredGroups"]], ["4@g.us"])
        self.assertEqual(self.m, before)

    def test_failure_stale_and_disabled_keep_saved_observations(self):
        self.save(lastError="lookup_timeout", lastAttemptAt=LATER)
        result = self.read()
        self.assertEqual(result["groupDiscovery"]["accounts"][0]["status"], "error")
        self.assertEqual(len(result["discoveredGroups"]), 1)
        self.save()
        stale = self.read(now=NOW + 7200)
        self.assertEqual(stale["groupDiscovery"]["accounts"][0]["status"], "stale")
        self.env["GENAIE_GROUP_DISCOVERY"] = "0"
        disabled = self.read()
        self.assertFalse(disabled["groupDiscovery"]["enabled"])
        self.assertEqual(disabled["groupDiscovery"]["accounts"][0]["status"], "disabled")
        self.assertEqual(len(disabled["discoveredGroups"]), 1)

    def test_bad_and_interrupted_files_do_not_break_other_account_or_leak_errors(self):
        self.save("default")
        (self.directory / "ads.json").write_text('{broken SECRET')
        (self.directory / "default.json.interrupted.tmp").write_text('{partial')
        result = self.read()
        self.assertEqual(len(result["discoveredGroups"]), 1)
        self.assertEqual(result["groupDiscovery"]["accounts"][0]["lastError"], "inventory_unavailable")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_invalid_records_are_reported_as_unavailable(self):
        cases = [{"version": 2}, {"accountId": "other"}, {"lastSuccessAt": "invalid"},
                 {"lastError": []}, {"groups": {"user@s.whatsapp.net": {}}},
                 {"groups": {"1@g.us": {"name": [], "firstSeenAt": STAMP, "lastSeenAt": STAMP}}},
                 {"groups": {"1@g.us": {"name": "\ud83d", "firstSeenAt": STAMP, "lastSeenAt": STAMP}}}]
        for fields in cases:
            with self.subTest(fields=fields):
                self.save(**fields)
                result = self.read()
                self.assertEqual(result["discoveredGroups"], [])
                self.assertEqual(result["groupDiscovery"]["accounts"][0]["status"], "error")

    def test_node_writer_and_python_reader_share_the_same_persistent_format(self):
        helper = (ROOT / "deploy/railway/whatsapp-group-discovery.mjs").as_uri()
        source = '''import {createWhatsAppGroupDiscovery} from %s;
const d=createWhatsAppGroupDiscovery({root:process.argv[1],accountId:'ads',now:()=>new Date(%s)});
d.start({groupFetchAllParticipating:async()=>({'9@g.us':{subject:'Live subject',participants:['private']}})});
await d.scan(); d.stop();''' % (json.dumps(helper), json.dumps(STAMP))
        result = subprocess.run(["node", "--input-type=module", "-e", source, self.temp.name],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        row, = self.read()["discoveredGroups"]
        self.assertEqual((row["jid"], row["name"]), ("9@g.us", "Live subject"))
        self.assertNotIn("private", json.dumps(row))

    def test_the_console_leaves_out_groups_the_config_already_knows(self):
        """A configured group is not "unassigned", whatever its status.

        This used to be asserted once per config source, when the console could
        be reading the committed membership file instead of the store. There is
        one source now, so what is left to pin is the thing that actually
        matters: an on-hold, admin-only or deliberately cleared record still
        suppresses the group, which is what keeps the Unassigned list a list of
        genuinely new groups."""
        loader = importlib.machinery.SourceFileLoader("discovery_console_test", str(ROOT / "workspace/bin/console-read"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        console = importlib.util.module_from_spec(spec)
        loader.exec_module(console)
        self.save(groups={"1@g.us": {"name": "Known", "firstSeenAt": STAMP, "lastSeenAt": STAMP},
                          "2@g.us": {"name": "New", "firstSeenAt": STAMP, "lastSeenAt": STAMP}})
        sys.path.insert(0, str(ROOT / "deploy"))
        import promo_fixture
        membership = copy.deepcopy(promo_fixture.membership())
        for status in ("on-hold", "admin-only", "not-a-member"):
            membership["groups"]["1@g.us"] = {"name": "Configured", "status": status, "promos": []}
            with self.subTest(status=status), patch.dict(os.environ, self.env), \
                 patch.object(console.promo_config, "resolve", return_value={
                     "membership": membership, "mode": "firestore", "source": "firestore",
                     "switches": {}, "storeSwitches": {}, "report": {}, "notes": []}), \
                 patch.object(console.sys, "stdout") as output:
                console._RESOLVED.clear()
                self.assertEqual(console.main(["groups", "--date", "2026-11-08"]), 0)
                raw = "".join(call.args[0] for call in output.write.call_args_list)
                payload = json.loads(raw)
                self.assertEqual([g["jid"] for g in payload["discoveredGroups"]], ["2@g.us"])
                self.assertFalse(any(g["jid"] == "2@g.us" for g in payload["groups"]))


if __name__ == "__main__":
    unittest.main()
