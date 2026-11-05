"""Offline delivery and historical-baseline checks; no real WhatsApp traffic."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workspace/lib"))
import promo_broadcast as pb


class ConnectivityTests(unittest.TestCase):
    def setUp(self):
        self.naps = []

    def test_ads_trial_restores_the_ten_group_29_combination_baseline(self):
        membership = json.loads((ROOT / "workspace/cron/membership.json").read_text())
        active = {jid: g for jid, g in membership["groups"].items()
                  if g.get("account", "default") == "ads" and g["status"] == "active"}
        self.assertEqual(len(active), 10)
        self.assertEqual(sum(len(g["promos"]) for g in active.values()), 29)
        for jid in ("120363046894354726@g.us", "120363433121439254@g.us"):
            self.assertEqual(membership["groups"][jid]["status"], "on-hold")
            self.assertEqual(len(membership["groups"][jid]["promos"]), 3)
            for targets in (ROOT / "workspace/cron").glob("*-promo/targets.json"):
                self.assertNotIn(jid, targets.read_text())

    def run_campaign(self, status, sender):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "image.jpg").write_bytes(b"fixture")
            (root / "targets.json").write_text(json.dumps({"groups": ["1@g.us", "2@g.us", "3@g.us"],
                "accounts": {"1@g.us": "ads", "2@g.us": "ads"}}))
            with patch.object(pb, "send_ad", side_effect=sender) as send:
                result = pb.broadcast(text="fixture", image=str(root / "image.jpg"),
                    targets_file=str(root / "targets.json"), state_file=str(root / "state.json"),
                    argv=[], start_jitter=0, cron_timeout=None, caps=None, send_delay=0,
                    account_status=status, sleep=self.naps.append)
            return result, json.loads((root / "state.json").read_text())["sent"], send.call_args_list

    def test_offline_ads_not_submitted_or_receipted_and_default_continues(self):
        result, sent, calls = self.run_campaign(lambda accounts: {"default": True, "ads": False},
                                               lambda *a, **k: (True, '{"messageId":"m"}'))
        self.assertEqual(result, 0)
        self.assertEqual(set(sent), {"3@g.us"})
        self.assertEqual([c.args[0] for c in calls], ["3@g.us"])

    def test_missing_status_defers_every_target_without_queueing(self):
        _, sent, calls = self.run_campaign(lambda accounts: {}, lambda *a, **k: self.fail("must not send"))
        self.assertEqual(sent, {})
        self.assertEqual(calls, [])

    def test_midrun_disconnect_preserves_uncertain_send_and_stops_more_ads(self):
        snapshots = iter([{"ads": True, "default": True}])
        def sender(target, *_args, **_kwargs):
            return ((False, "No active WhatsApp Web listener (account: ads)") if target == "1@g.us"
                    else (True, '{"messageId":"m"}'))
        result, sent, calls = self.run_campaign(lambda _: next(snapshots, {"ads": False}), sender)
        self.assertEqual(result, 0)
        self.assertEqual(set(sent), {"1@g.us", "3@g.us"})
        self.assertEqual([c.args[0] for c in calls], ["1@g.us", "3@g.us"])
        # It waited out the reconnect grace (and no longer) before giving up on ads.
        self.assertEqual(sum(self.naps), pb.DEFAULT_RECONNECT_GRACE)

    def test_routine_reconnect_mid_run_does_not_defer_the_rest_of_the_line(self):
        # 2026-11-05: a status-500 relink took <1 s, but the send that hit it
        # deferred every later group on that line for the day.
        snapshots = iter([{"ads": True, "default": True}, {"ads": False}, {"ads": False}])
        def sender(target, *_args, **_kwargs):
            return ((False, "No active WhatsApp Web listener (account: ads)") if target == "1@g.us"
                    else (True, '{"messageId":"m"}'))
        result, sent, calls = self.run_campaign(lambda _: next(snapshots, {"ads": True}), sender)
        self.assertEqual(result, 0)
        self.assertEqual([c.args[0] for c in calls], ["1@g.us", "2@g.us", "3@g.us"])
        self.assertEqual(set(sent), {"1@g.us", "2@g.us", "3@g.us"})
        self.assertEqual([n for n in self.naps if n], [pb.RECONNECT_POLL_SECONDS] * 2)

    def test_zero_grace_restores_immediate_deferral(self):
        calls = []
        def status(accounts):
            calls.append(accounts)
            return {"ads": False}
        with patch.dict("os.environ", {"PROMO_RECONNECT_GRACE_SECONDS": "0"}):
            self.assertFalse(pb.wait_for_account(status, "ads", sleep=self.fail))
        self.assertEqual(len(calls), 1)

    def test_status_requires_explicit_connected_account_and_never_probes_phone(self):
        payload = {"channelAccounts": {"whatsapp": [{"accountId": "default", "connected": True},
                    {"accountId": "ads", "connected": False}]}}
        with patch.object(pb, "active_client", return_value=None), patch.object(pb.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0, json.dumps(payload))) as run:
            self.assertEqual(pb.account_connectivity({"default", "ads"}), {"default": True, "ads": False})
            self.assertEqual(run.call_args.args[0], ["openclaw", "channels", "status", "--json"])
        for payload in ({}, {"channelAccounts": {"whatsapp": None}}):
            with patch.object(pb, "active_client", return_value=None), patch.object(pb.subprocess, "run",
                    return_value=subprocess.CompletedProcess([], 0, json.dumps(payload))):
                self.assertEqual(pb.account_connectivity({"ads"}), {"ads": False})

    def test_rpc_status_uses_existing_gateway_transport_without_a_send(self):
        from unittest.mock import Mock
        client = Mock()
        client.request.return_value = (True, json.dumps({"channelAccounts": {"whatsapp": [
            {"accountId": "ads", "connected": True}]}}))
        with patch.object(pb, "active_client", return_value=client):
            self.assertEqual(pb.account_connectivity({"ads"}), {"ads": True})
        client.request.assert_called_once_with("channels.status", {})


if __name__ == "__main__":
    unittest.main()
