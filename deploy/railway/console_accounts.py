"""Bounded, serialized access to the persistent gateway account-control bridge."""
import json
import os
from pathlib import Path
import select
import subprocess
import threading
import time


class AccountControls:
    def __init__(self, bridge=None, response_timeout=55):
        state = os.environ.get("OPENCLAW_STATE_DIR") or "/home/joseph/.openclaw"
        self.bridge = bridge or str(Path(state) / "workspace/lib/console_accounts.mjs")
        self.response_timeout = response_timeout
        self.lock = threading.Lock()
        self.process = None

    def close(self):
        process, self.process = self.process, None
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            process.stdin.close()
            process.stdout.close()

    def run(self, request):
        if not self.lock.acquire(blocking=False):
            return 409, b'{"error":"An account operation is in progress. Refresh status before retrying."}'
        try:
            if self.process is None or self.process.poll() is not None:
                self.close()
                # argv only; never a shell, and never a credential on the command line.
                self.process = subprocess.Popen(
                    ["node", self.bridge], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
            self.process.stdin.write(json.dumps(request).encode() + b"\n")
            self.process.stdin.flush()
            deadline = time.monotonic() + self.response_timeout
            line = bytearray()
            while b"\n" not in line:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                    raise TimeoutError()
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk or len(line) + len(chunk) > 1024 * 1024:
                    raise ValueError()
                line.extend(chunk)
            if line.count(b"\n") != 1 or not line.endswith(b"\n"):
                raise ValueError()
            result = json.loads(line)
            if not isinstance(result, dict):
                raise ValueError()
            if result.get("ok") is True:
                return 200, json.dumps(result["payload"]).encode()
            status = result.get("status", 502)
            if status not in (400, 404, 409, 502, 503, 504):
                status = 502
            return status, json.dumps({"error": result.get("error", "Account operation failed.")}).encode()
        except (OSError, ValueError, KeyError, TimeoutError):
            self.close()
            return 503, b'{"error":"Account controls are unavailable. Check gateway availability and refresh status before retrying."}'
        finally:
            self.lock.release()
