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
    def __init__(self, namespace, timeout=180, keep_alive=True):
        self.namespace = namespace
        self.timeout = timeout
        self.process = None
        self.buffer = b""
        self.sequence = 0
        self.keep_alive = keep_alive

    def close(self):
        process, self.process = self.process, None
        self.buffer = b""
        if process is None:
            return
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                process.terminate()
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            except OSError:
                pass
        except OSError:
            pass
        finally:
            try:
                process.stdout.close()
            except OSError:
                pass

    def _remaining(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("gateway acknowledgement timed out; delivery outcome unknown")
        return remaining

    def _write(self, data, deadline):
        pending = memoryview(data)
        while pending:
            if not select.select([], [self.process.stdin], [], self._remaining(deadline))[1]:
                raise TimeoutError("gateway write timed out; delivery outcome unknown")
            try:
                written = os.write(self.process.stdin.fileno(), pending)
            except BlockingIOError:
                continue
            if not written:
                raise RuntimeError("gateway helper closed during write; delivery outcome unknown")
            pending = pending[written:]

    def request(self, method, params):
        # Startup failure is terminal for this request too. Switching transports
        # after a timeout/closed pipe could duplicate a request already accepted.
        try:
            deadline = time.monotonic() + self.timeout
            if self.process is None:
                self.buffer = b""
                self.process = subprocess.Popen(
                    ["node", str(Path(__file__).with_suffix(".mjs"))],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=None, env=os.environ.copy(), bufsize=0,
                )
                os.set_blocking(self.process.stdin.fileno(), False)
                os.set_blocking(self.process.stdout.fileno(), False)
            self.sequence += 1
            identifier = str(self.sequence)
            message = json.dumps({"id": identifier, "method": method, "params": params})
            self._write(message.encode() + b"\n", deadline)
            while b"\n" not in self.buffer:
                if not select.select([self.process.stdout], [], [], self._remaining(deadline))[0]:
                    raise TimeoutError("gateway acknowledgement timed out; delivery outcome unknown")
                try:
                    chunk = os.read(self.process.stdout.fileno(), 65536)
                except BlockingIOError:
                    continue
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
            try:
                self.close()
            except Exception:
                # Cleanup must not replace the delivery failure being reported.
                pass
            return False, "gateway transport error: %s" % error
        finally:
            if not self.keep_alive:
                try:
                    self.close()
                except Exception:
                    # A confirmed acknowledgement remains confirmed even when
                    # helper cleanup fails. Never resend or switch transports.
                    pass

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
        transport = os.environ.get("GENAIE_CAMPAIGN_TRANSPORT", "cli")
        if transport not in {"rpc", "rpc-once"}:
            return function(*args, **kwargs)
        from datetime import date
        namespace = "%s:%s" % (kwargs.get("state_file", ""), date.today().isoformat())
        client = CampaignClient(namespace, keep_alive=transport == "rpc")
        token = _client.set(client)
        try:
            return function(*args, **kwargs)
        finally:
            _client.reset(token)
            try:
                client.close()
            except Exception as error:
                # A cleanup fault must not overwrite a broadcast's original error.
                import sys
                print("gateway helper cleanup failed: %s" % error, file=sys.stderr)
    return wrapped


def active_client():
    return _client.get()
