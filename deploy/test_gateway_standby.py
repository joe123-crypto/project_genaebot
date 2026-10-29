import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("standby", ROOT / "deploy/railway/gateway-standby.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)


class StandbyTests(unittest.TestCase):
    def test_schedule_boundaries_in_utc_and_algeria(self):
        for value, expected in [("2026-10-29T15:59:59+00:00", None),
                                ("2026-10-29T16:00:00+00:00", "2026-10-30T05:45:00+01:00"),
                                ("2026-10-30T00:00:00+00:00", "2026-10-30T05:45:00+01:00"),
                                ("2026-10-30T04:44:59+00:00", "2026-10-30T05:45:00+01:00"),
                                ("2026-10-30T04:45:00+00:00", None)]:
            wake = S.standby_window(datetime.fromisoformat(value))
            self.assertEqual(wake.isoformat() if wake else None, expected)

    def fixture(self):
        now = datetime(2026, 10, 29, 17, 1, tzinfo=S.ZONE)
        wake = S.standby_window(now)
        return ({"lanes": [{"activeCount": 0, "queuedCount": 0}], "dynamic": None},
                {"jobs": [{"name": "fixture-promo", "enabled": True,
                           "state": {"nextRunAtMs": (wake + timedelta(minutes=30)).timestamp() * 1000}}]},
                {"pid": os.getpid(), "active": 0, "updatedAtMs": (now.timestamp() - 120) * 1000}, now, wake)

    def test_active_lanes_requests_crons_and_changed_schedule_defer_standby(self):
        self.assertTrue(S.validate_activity(*self.fixture())[0])
        for section, key, value in [(0, "activeCount", 1), (0, "queuedCount", 1),
                                    (2, "active", 1), (1, "runningAtMs", 1),
                                    (1, "nextRunAtMs", 1)]:
            data = self.fixture()
            target = data[0]["lanes"][0] if section == 0 else data[1]["jobs"][0]["state"] if section == 1 else data[2]
            target[key] = value
            self.assertFalse(S.validate_activity(*data)[0])

    def test_unverifiable_activity_and_native_receipts_keep_gateway_awake(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lanes, cron, requests, now, wake = self.fixture()
            (root / "requests.json").write_text(json.dumps(requests))
            (root / "state").mkdir()
            with sqlite3.connect(root / "state/openclaw.sqlite") as database:
                database.execute("CREATE TABLE cron_run_receipts(status TEXT)")
                database.execute("CREATE TABLE delivery_queue_entries(status TEXT)")
                database.execute("INSERT INTO cron_run_receipts VALUES ('running')")
            with patch.object(S, "gateway_call", side_effect=[lanes, cron]):
                self.assertFalse(S.can_stop(root, root, now, wake)[0])
            with sqlite3.connect(root / "state/openclaw.sqlite") as database:
                database.execute("UPDATE cron_run_receipts SET status='ok'")
            with patch.object(S, "gateway_call", side_effect=[lanes, cron]):
                self.assertTrue(S.can_stop(root, root, now, wake)[0])
            with sqlite3.connect(root / "state/openclaw.sqlite") as database:
                database.execute("INSERT INTO delivery_queue_entries VALUES ('pending')")
            with patch.object(S, "gateway_call", side_effect=[lanes, cron]):
                self.assertFalse(S.can_stop(root, root, now, wake)[0])
            for bad in [{}, {"lanes": []}, {"lanes": [{"activeCount": "0", "queuedCount": 0}]}]:
                with patch.object(S, "gateway_call", side_effect=[bad, cron]):
                    self.assertFalse(S.can_stop(root, root, now, wake)[0])
            (root / "requests.json").unlink()
            self.assertFalse(S.can_stop(root, root, now, wake)[0])

    def test_disabled_exec_preserves_command_exit_status(self):
        environment = {**os.environ, "GENAIE_OVERNIGHT_STANDBY": "0"}
        result = subprocess.run([sys.executable, str(ROOT / "deploy/railway/gateway-standby.py"),
                                 "--port", "18789", "--", sys.executable, "-c", "raise SystemExit(7)"],
                                env=environment, timeout=10)
        self.assertEqual(result.returncode, 7)

    def test_real_child_two_sleep_wake_cycles_preserve_state_and_release_health_port(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            counter = root / "counter.json"
            ready = root / "release-ready"
            ready.touch()
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            child_code = '''import json,signal,time,sys
from pathlib import Path
from http.server import HTTPServer,BaseHTTPRequestHandler
p=Path(sys.argv[1]);d=json.loads(p.read_text()) if p.exists() else {"starts":0,"stops":0,"data":"preserved"}
d["starts"]+=1
server=HTTPServer(("0.0.0.0",int(sys.argv[2])),BaseHTTPRequestHandler)
def stop(*args):
 d["stops"]+=1;p.write_text(json.dumps(d));server.server_close();raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
p.write_text(json.dumps(d))
while True:time.sleep(.02)
'''
            current = [datetime(2026, 10, 29, 17, 0, tzinfo=S.ZONE)]
            ticks = [0]
            health_seen = []
            second_night = [False]
            children = []
            deadline = time.monotonic() + 15
            def launch(command):
                child = subprocess.Popen(command)
                children.append(child)
                return child
            def wait(_seconds):
                time.sleep(.03)
                ticks[0] += 61
                if time.monotonic() > deadline:
                    raise TimeoutError(f"cycle did not finish: {counter.read_text()}, {current[0]}, health={health_seen}")
                try:
                    data = json.loads(counter.read_text())
                except (OSError, ValueError):
                    return
                if data["starts"] > data["stops"]:
                    current[0] += timedelta(seconds=61)
                elif data["stops"] in (1, 2) and data["stops"] not in health_seen:
                    try:
                        with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=.1) as response:
                            body = json.load(response)
                        if body["mode"] != "overnight-standby":
                            return
                        with self.assertRaises(HTTPError) as unavailable:
                            urlopen(f"http://127.0.0.1:{port}/", timeout=.1)
                        self.assertEqual(unavailable.exception.code, 503)
                        unavailable.exception.close()
                    except OSError:
                        return
                    health_seen.append(data["stops"])
                    current[0] = current[0].replace(hour=5, minute=45) + timedelta(days=1)
                if data["starts"] == 2 and data["stops"] == 1 and not second_night[0]:
                    current[0] = current[0].replace(hour=17, minute=0)
                    second_night[0] = True
                if data["starts"] == 3:
                    signal.raise_signal(signal.SIGTERM)
            try:
                result = S.supervise([sys.executable, "-c", child_code, str(counter), str(port)], root, root, port,
                    clock=lambda: current[0], wait=wait, idle_check=lambda *_args: (True, "idle"),
                    launch=launch, ready_file=ready, monotonic=lambda: ticks[0])
                self.assertEqual(result, 0)
                self.assertEqual(health_seen, [1, 2])
                self.assertEqual(json.loads(counter.read_text()), {"starts": 3, "stops": 3, "data": "preserved"})
                self.assertTrue(all(child.poll() is not None for child in children))
            finally:
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                        child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
