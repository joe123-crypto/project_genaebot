"""Offline installed-runtime smoke: real gateway ownership across two sleeps."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("standby_runtime", ROOT / "deploy/railway/gateway-standby.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)


@unittest.skipUnless(os.environ.get("GENAIE_OPENCLAW_ROOT"), "installed runtime is exercised in the native CI job")
class RuntimeTests(unittest.TestCase):
    def test_real_gateway_restarts_with_same_native_state_after_two_standbys(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            state = home / "state"
            state.mkdir()
            plugin = home / "public-service-gate"
            shutil.copytree(ROOT / "workspace/plugins/public-service-gate", plugin)
            marker = state / "preserved-fixture"
            marker.write_text("preserved")
            ready = home / "release-ready"
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            config = {"gateway": {"mode": "local", "bind": "loopback", "auth": {
                "mode": "token", "token": "offline-standby-fixture"}}, "channels": {},
                "plugins": {"allow": ["public-service-gate"],
                    "load": {"paths": [str(plugin)]},
                    "entries": {"public-service-gate": {"enabled": True}}}, "cron": {"enabled": False}}
            (state / "openclaw.json").write_text(json.dumps(config))
            environment = {**os.environ, "HOME": str(home), "OPENCLAW_STATE_DIR": str(state),
                           "OPENCLAW_CONFIG_PATH": str(state / "openclaw.json"),
                           "OPENCLAW_NO_AUTO_UPDATE": "1", "GENAIE_OVERNIGHT_STANDBY": "1",
                           "GENAIE_STANDBY_RUNTIME": str(home)}
            environment.pop("OPENCLAW_HOME", None)
            children, logs = [], []
            handles = []
            slept = []
            now = [datetime(2026, 10, 29, 17, 0, tzinfo=S.ZONE)]
            ticks = [0]
            online = set()
            deadline = time.monotonic() + 180
            def launch(command):
                log_path = home / f"gateway-{len(children)}.log"
                handle = log_path.open("w")
                handles.append(handle)
                child = subprocess.Popen(command, env=environment, stdout=handle, stderr=subprocess.STDOUT)
                children.append(child)
                logs.append(log_path)
                return child
            def idle(*_args):
                return ("[gateway] ready" in logs[-1].read_text(), "waiting for real gateway startup")
            def wait(_seconds):
                time.sleep(.1)
                ticks[0] += 61
                if time.monotonic() > deadline:
                    raise TimeoutError("native standby did not recover: " + logs[-1].read_text()[-1500:])
                if children and children[-1].poll() is not None and len(slept) < len(children):
                    try:
                        with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=.2) as response:
                            mode = json.load(response).get("mode")
                    except (OSError, ValueError):
                        return
                    if mode == "overnight-standby":
                        slept.append(len(children))
                        # Native helpers must also leave; lingering workers would
                        # retain memory and database custody during standby.
                        remaining = []
                        for proc in Path("/proc").glob("[0-9]*"):
                            try:
                                comm = (proc / "comm").read_text().strip()
                                status = (proc / "status").read_text()
                                owned = f"OPENCLAW_STATE_DIR={state}".encode() in (proc / "environ").read_bytes().split(b"\0")
                                if owned and (comm.startswith("node") or comm.startswith("openclaw")) and "State:\tZ" not in status:
                                    remaining.append(comm)
                            except OSError:
                                pass
                        self.assertEqual(remaining, [])
                        self.assertEqual(marker.read_text(), "preserved")
                        self.assertTrue((state / "state/openclaw.sqlite").is_file())
                        now[0] = now[0].replace(hour=5, minute=45) + timedelta(days=1)
                        return
                if children and "[gateway] ready" in logs[-1].read_text():
                    number = len(children)
                    if number not in online:
                        if not (home / "requests.json").exists():
                            if ticks[0] > 60 * 60:
                                self.fail("request tracker did not initialize: " + logs[-1].read_text()[-3500:])
                            return
                        tracking = json.loads((home / "requests.json").read_text())
                        self.assertEqual(tracking["active"], 0)
                        os.kill(tracking["pid"], 0)
                        online.add(number)
                        ready.touch()
                        now[0] = now[0].replace(hour=17, minute=0)
                    else:
                        now[0] += timedelta(seconds=61)
                    if number == 3:
                        signal.raise_signal(signal.SIGTERM)
            try:
                result = S.supervise(["openclaw", "gateway", "--allow-unconfigured", "--port", str(port),
                                      "--bind", "loopback", "--tailscale", "off"],
                    home, state, port, clock=lambda: now[0], wait=wait, idle_check=idle,
                    launch=launch, ready_file=ready, monotonic=lambda: ticks[0])
                self.assertEqual(result, 0)
                self.assertEqual(slept, [1, 2])
                self.assertEqual(online, {1, 2, 3})
                self.assertTrue(all(child.returncode == 0 for child in children))
                self.assertEqual(marker.read_text(), "preserved")
            finally:
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                        try:
                            child.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=5)
                for handle in handles:
                    handle.close()


if __name__ == "__main__":
    unittest.main()
