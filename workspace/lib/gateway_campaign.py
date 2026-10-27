"""One lazily started gateway helper per campaign; never retry an uncertain send."""

from contextvars import ContextVar
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import time

_client = ContextVar("campaign_gateway_client", default=None)


class CampaignClient:
    def __init__(self, namespace, timeout=180):
        self.namespace = namespace
        self.timeout = timeout
        self.process = None
        self.buffer = b""
        self.sequence = 0

    def close(self):
        if self.process is None:
            return
        process, self.process = self.process, None
        try:
            process.stdin.close()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        finally:
            process.stdout.close()

    def request(self, method, params):
        # Startup failure is terminal for this request too. Switching transports
        # after a timeout/closed pipe could duplicate a request already accepted.
        try:
            if self.process is None:
                self.process = subprocess.Popen(
                    ["node", str(Path(__file__).with_suffix(".mjs"))],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=None, env=os.environ.copy(),
                )
            self.sequence += 1
            identifier = str(self.sequence)
            message = json.dumps({"id": identifier, "method": method, "params": params})
            self.process.stdin.write(message.encode() + b"\n")
            self.process.stdin.flush()
            deadline = time.monotonic() + self.timeout
            while b"\n" not in self.buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                    raise TimeoutError("gateway acknowledgement timed out; delivery outcome unknown")
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("gateway helper closed; delivery outcome unknown")
                self.buffer += chunk
                if len(self.buffer) > 1024 * 1024:
                    raise RuntimeError("gateway helper response exceeded limit")
            line, self.buffer = self.buffer.split(b"\n", 1)
            response = json.loads(line)
            if response.get("id") != identifier:
                raise RuntimeError("gateway helper response ID mismatch")
            if response.get("ok") is not True:
                return False, json.dumps({"error": response.get("error", "gateway request failed")})
            return True, json.dumps(response.get("payload", {}))
        except Exception as error:
            self.close()
            return False, "gateway transport error: %s" % error

    def send(self, target, text, image, account=None):
        params = {"channel": "whatsapp", "to": target, "message": text, "mediaUrl": image}
        if account and account != "default":
            params["accountId"] = account
        identity = json.dumps([self.namespace, account or "default", target, text, image],
                              ensure_ascii=False, separators=(",", ":"))
        params["idempotencyKey"] = "campaign-" + hashlib.sha256(identity.encode()).hexdigest()
        ok, output = self.request("send", params)
        if ok:
            payload = json.loads(output)
            ok = isinstance(payload, dict) and bool(payload.get("messageId"))
        return ok, output


def campaign_transport(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if os.environ.get("GENAIE_CAMPAIGN_TRANSPORT", "cli") != "rpc":
            return function(*args, **kwargs)
        from datetime import date
        namespace = "%s:%s" % (kwargs.get("state_file", ""), date.today().isoformat())
        client = CampaignClient(namespace)
        token = _client.set(client)
        try:
            return function(*args, **kwargs)
        finally:
            _client.reset(token)
            client.close()
    return wrapped


def active_client():
    return _client.get()
