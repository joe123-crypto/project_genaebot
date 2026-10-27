import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "workspace/lib"))
import gateway_campaign as gc
import promo_broadcast as pb


class CampaignGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = self.root / "calls.jsonl"
        self.helper = self.root / "helper.py"
        self.helper.write_text('''import sys,json,time
for line in sys.stdin:
 d=json.loads(line)
 with open(sys.argv[1],"a") as f: f.write(json.dumps(d)+"\\n")
 if d["params"].get("message")=="timeout": time.sleep(10)
 if d["params"].get("message")=="queued":
  response={"id":d["id"],"ok":False,"error":{"message":"No active WhatsApp Web listener"}}
 else:
  response={"id":d["id"],"ok":True,"payload":{"messageId":"confirmed"}}
 print(json.dumps(response),flush=True)
''')
        self.processes = []
        original = subprocess.Popen

        def start(command, **kwargs):
            self.assertEqual(command[0], "node")
            process = original([sys.executable, str(self.helper), str(self.log)], **kwargs)
            self.processes.append(process)
            return process
        self.mock = patch.object(gc.subprocess, "Popen", side_effect=start)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_reuses_process_and_retains_default_and_ads_routing(self):
        client = gc.CampaignClient("promo:2026-10-27")
        self.addCleanup(client.close)
        self.assertTrue(client.send("1@g.us", "hello", "/ad.png")[0])
        self.assertTrue(client.send("2@g.us", "hello", "/ad.png", "ads")[0])
        self.assertEqual(len(self.processes), 1)
        first, second = self.calls()
        self.assertNotIn("accountId", first["params"])
        self.assertEqual(second["params"]["accountId"], "ads")
        self.assertEqual(second["params"]["mediaUrl"], "/ad.png")
        self.assertNotEqual(first["params"]["idempotencyKey"], second["params"]["idempotencyKey"])
        client.close()
        self.assertIsNotNone(self.processes[0].poll())

    def test_retry_identity_survives_a_helper_restart(self):
        for _ in range(2):
            client = gc.CampaignClient("same-promo:same-day")
            self.assertTrue(client.send("1@g.us", "hello", "/ad.png", "ads")[0])
            client.close()
        a, b = self.calls()
        self.assertEqual(a["params"]["idempotencyKey"], b["params"]["idempotencyKey"])

    def test_corrupt_trailing_data_is_discarded_before_a_healthy_restart(self):
        self.helper.write_text('''import sys,json
for line in sys.stdin:
 d=json.loads(line)
 if d['id']=='1': print('bad-json\\n'+json.dumps({'id':'stale','ok':True}),flush=True)
 else: print(json.dumps({'id':d['id'],'ok':True,'payload':{'messageId':'healthy'}}),flush=True)
''')
        client = gc.CampaignClient("recovery")
        self.addCleanup(client.close)
        self.assertFalse(client.send("1@g.us", "hello", "/ad.png")[0])
        self.assertEqual(client.buffer, b"")
        self.assertTrue(client.send("2@g.us", "hello", "/ad.png")[0])
        self.assertEqual(len(self.processes), 2)

    def test_wrong_id_eof_and_oversize_responses_are_bounded_and_clear_buffers(self):
        for response in ['print(\'{"id":"wrong","ok":true}\',flush=True)',
                         'sys.exit(0)', 'print("x"*(1024*1024+1),flush=True)']:
            with self.subTest(response=response):
                self.helper.write_text('import sys\nsys.stdin.readline()\n'+response+'\n')
                client = gc.CampaignClient("fault", timeout=0.2)
                ok, output = client.send("1@g.us", "hello", "/ad.png")
                self.assertFalse(ok)
                self.assertIn("gateway transport error", output)
                self.assertIsNone(client.process)
                self.assertEqual(client.buffer, b"")

    def test_partial_response_is_reassembled(self):
        self.helper.write_text('''import sys,json,time
for line in sys.stdin:
 d=json.loads(line); response=json.dumps({'id':d['id'],'ok':True,'payload':{'messageId':'partial'}})+'\\n'
 sys.stdout.write(response[:10]);sys.stdout.flush();time.sleep(.02)
 sys.stdout.write(response[10:]);sys.stdout.flush()
''')
        client = gc.CampaignClient("partial")
        self.addCleanup(client.close)
        self.assertTrue(client.send("1@g.us", "hello", "/ad.png")[0])

    def test_blocked_write_obeys_the_request_deadline(self):
        self.helper.write_text('import time\ntime.sleep(10)\n')
        client = gc.CampaignClient("blocked-write", timeout=0.05)
        ok, output = client.send("1@g.us", "x" * 200_000, "/ad.png")
        self.assertFalse(ok)
        self.assertIn("write timed out", output)
        self.assertIsNone(client.process)

    def test_timeout_is_not_replayed_or_fallen_back_to_cli(self):
        client = gc.CampaignClient("promo", timeout=0.15)
        ok, output = client.send("1@g.us", "timeout", "/ad.png")
        self.assertFalse(ok)
        self.assertIn("outcome unknown", output)
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(len(self.calls()), 1)
        self.assertIsNotNone(self.processes[0].poll())

    def test_dry_run_never_starts_the_helper(self):
        image = self.root / "ad.png"
        image.write_bytes(b"fixture")
        targets = self.root / "targets.json"
        targets.write_text('{"groups":["1@g.us"]}')
        with patch.dict(os.environ, {"GENAIE_CAMPAIGN_TRANSPORT": "rpc"}):
            result = pb.broadcast(text="hello", image=str(image), targets_file=str(targets),
                                  state_file=str(self.root / "state.json"),
                                  argv=["--dry-run"], caps=None)
        self.assertEqual(result, 0)
        self.assertEqual(self.processes, [])
        self.assertFalse((self.root / "state.json").exists())

    def test_rpc_broadcast_commits_each_group_and_skips_completed_reruns(self):
        image = self.root / "ad.png"
        image.write_bytes(b"fixture")
        targets = self.root / "targets.json"
        targets.write_text('{"groups":["1@g.us","2@g.us"]}')
        state = self.root / "state.json"
        arguments = dict(text="hello", image=str(image), targets_file=str(targets),
                         state_file=str(state), argv=[], caps=None, cron_timeout=None,
                         start_jitter=0, send_delay=0)
        with patch.dict(os.environ, {"GENAIE_CAMPAIGN_TRANSPORT": "rpc"}):
            self.assertEqual(pb.broadcast(**arguments), 0)
            self.assertEqual(pb.broadcast(**arguments), 0)
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(set(json.loads(state.read_text())["sent"]), {"1@g.us", "2@g.us"})
        self.assertIsNotNone(self.processes[0].poll())

    def test_rpc_reconnect_queue_is_committed_without_a_resend(self):
        image = self.root / "ad.png"
        image.write_bytes(b"fixture")
        targets = self.root / "targets.json"
        targets.write_text('{"groups":["1@g.us"]}')
        state = self.root / "state.json"
        with patch.dict(os.environ, {"GENAIE_CAMPAIGN_TRANSPORT": "rpc"}):
            self.assertEqual(pb.broadcast(text="queued", image=str(image),
                targets_file=str(targets), state_file=str(state), argv=[], caps=None,
                cron_timeout=None, start_jitter=0, send_delay=0), 0)
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("1@g.us", json.loads(state.read_text())["sent"])


if __name__ == "__main__":
    unittest.main()
