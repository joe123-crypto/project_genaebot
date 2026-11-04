#!/usr/bin/env python3
"""Approved scheduled standby; never a spending-triggered shutdown.

Keep the container alive with a small health responder, then start the same
gateway command each morning. State, accounts and cron definitions are untouched.
Unknown activity or failed probes defer standby; no timeout kills active work.
"""

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import threading
import time
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Africa/Algiers")
QUIET_SECONDS = 60


def standby_window(now):
    """Return next wake inside the approved window, otherwise None."""
    local = now.astimezone(ZONE)
    wake = local.replace(hour=5, minute=45, second=0, microsecond=0)
    if (local.hour, local.minute) >= (17, 0):
        return wake + timedelta(days=1)
    return wake if local < wake else None


def validate_activity(lanes, cron, requests, now, wake):
    """Strict schemas keep unknown/new runtime shapes awake for review."""
    rows = lanes["lanes"]
    if lanes.get("dynamic") is not None:
        raise ValueError("unreviewed dynamic activity")
    if not isinstance(rows, list) or not rows:
        raise ValueError("missing activity lanes")
    for row in rows:
        for key in ("activeCount", "queuedCount"):
            count = row[key]
            if type(count) is not int or count < 0:
                raise ValueError("unknown lane activity")
            if count:
                return False, "gateway work remains active"
    if type(requests["active"]) is not int or requests["active"] != 0:
        return False, "request work remains active"
    updated = requests["updatedAtMs"]
    if not isinstance(updated, (int, float)) or not 0 <= now.timestamp() * 1000 - updated:
        raise ValueError("unknown request activity")
    if now.timestamp() * 1000 - updated < QUIET_SECONDS * 1000:
        return False, "waiting for a quiet request interval"
    jobs = cron["jobs"]
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("missing cron state")
    for job in jobs:
        if type(job["enabled"]) is not bool or not isinstance(job["name"], str):
            raise ValueError("unknown cron state")
        if not job["enabled"]:
            continue
        state = job["state"]
        if state.get("runningAtMs"):
            return False, "a cron is still running"
        if job["name"].endswith(("-promo", "-feed")):
            next_run = state["nextRunAtMs"]
            if type(next_run) not in (int, float) or next_run <= wake.timestamp() * 1000:
                return False, "a campaign is due before morning wake"
    return True, "idle"


def gateway_call(method, params=None):
    environment = dict(os.environ)
    environment.pop("OPENCLAW_HOME", None)
    environment["OPENCLAW_GATEWAY_TOKEN"] = environment.get("GATEWAY_AUTH_TOKEN", "")
    result = subprocess.run(["openclaw", "gateway", "call", method, "--json",
                             "--params", json.dumps(params or {}), "--timeout", "10000"],
                            env=environment, capture_output=True, text=True, check=True, timeout=20)
    return json.loads(result.stdout)


def can_stop(directory, state, now, wake):
    try:
        requests = json.loads((directory / "requests.json").read_text())
        if type(requests["pid"]) is not int or requests["pid"] <= 0:
            raise ValueError("unknown request owner")
        os.kill(requests["pid"], 0)
        lanes = gateway_call("diagnostics.lanes")
        cron = gateway_call("cron.list", {"includeDisabled": True})
        idle, reason = validate_activity(lanes, cron, requests, now, wake)
        if not idle:
            return idle, reason
        # Native receipts also cover shell campaigns outside an agent lane.
        with closing(sqlite3.connect(f"file:{state / 'state/openclaw.sqlite'}?mode=ro", uri=True,
                                     timeout=5)) as database:
            if database.execute("SELECT 1 FROM cron_run_receipts WHERE status='running' LIMIT 1").fetchone():
                return False, "a campaign receipt remains active"
            if database.execute("SELECT 1 FROM delivery_queue_entries WHERE status='pending' LIMIT 1").fetchone():
                return False, "outbound delivery remains pending"
        for name in ("delivery-queue", "session-delivery-queue"):
            if any((state / name).glob("*.json")):
                return False, "legacy outbound delivery remains pending"
        # Re-read after RPCs: a CV helper may have started while probes ran.
        latest = json.loads((directory / "requests.json").read_text())
        if latest != requests:
            return False, "request activity changed during verification"
        return True, reason
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError):
        return False, "activity could not be verified; staying awake"


class StandbyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        health = self.path.split("?", 1)[0] in {"/health", "/healthz", "/ready", "/readyz"}
        body = json.dumps({"mode": "overnight-standby", "gatewayAvailable": False,
                           "wake": "05:45 Africa/Algiers"}).encode()
        self.send_response(200 if health else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class HealthServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def log(message):
    print("[standby] " + message, flush=True)


def audit_lifecycle(state, event, reason, planned, now):
    """Append planned process transitions to the same private lifecycle journal.

    The adapter records socket events. These process records explain whether a
    socket stop coincided with standby, a deployment signal, or an unexpected exit.
    """
    if os.environ.get("GENAIE_WHATSAPP_HEALTH_AUDIT") == "0":
        return
    try:
        config = json.loads((state / "openclaw.json").read_text())
        whatsapp = config.get("channels", {}).get("whatsapp", {})
        accounts = whatsapp.get("accounts", {"default": {}})
        stamp = now.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        directory = state / "runtime/whatsapp-health"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        deployment = os.environ.get("RAILWAY_DEPLOYMENT_ID", "")
        deployment = deployment if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", deployment) else None
        for account, settings in accounts.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", account) or not settings.get("enabled", True):
                continue
            record = {"timestamp": stamp, "accountId": account, "deploymentId": deployment,
                      "runId": None, "connectionId": None, "event": event, "reason": reason,
                      "statusCode": None, "planned": planned}
            body = (json.dumps(record) + "\n").encode()
            fd = os.open(directory / f"events-{stamp[:10]}.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                if os.fstat(fd).st_size + len(body) <= 1024 * 1024:
                    os.write(fd, body)
            finally:
                os.close(fd)
    except (OSError, ValueError, TypeError, AttributeError):
        # Missing configuration during an offline test or unavailable audit storage
        # must not affect supervision or expose configuration values.
        pass


def supervise(command, directory, state, port, *, clock=None, wait=None, idle_check=can_stop,
              launch=subprocess.Popen, health_factory=HealthServer, ready_file=None,
              monotonic=time.monotonic, audit=audit_lifecycle):
    clock = clock or (lambda: datetime.now(ZONE))
    stopping = threading.Event()
    wait = wait or stopping.wait
    old_handlers = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        old_handlers[signum] = signal.signal(signum, lambda *_args: stopping.set())
    child = None
    server = None
    health_thread = None
    draining = False
    closed_at = None
    next_probe = 0
    last_reason = None
    first_boot = True
    closing_file = directory / "closing"

    def close_health():
        nonlocal server, health_thread
        if server:
            server.shutdown()
            server.server_close()
            health_thread.join(timeout=5)
            server = health_thread = None

    try:
        while not stopping.is_set():
            now = clock()
            wake = standby_window(now)
            if child is None and (first_boot or wake is None):
                close_health()
                closing_file.unlink(missing_ok=True)
                (directory / "requests.json").unlink(missing_ok=True)
                child = launch(command)
                audit(state, "gateway_start", "startup" if first_boot else "scheduled_wake", not first_boot, now)
                first_boot = False
                draining = False
                log("gateway started; automatic morning wake is local to this container")
            if child is not None and child.poll() is not None:
                code = child.returncode
                child = None
                if not draining:
                    audit(state, "gateway_stopped", "unexpected_exit", False, now)
                    log("gateway exited unexpectedly; handing recovery to the platform")
                    return code or 1
                audit(state, "gateway_stopped", "overnight_standby", True, now)
                draining = False
                # If graceful shutdown crossed wake time, restart immediately.
                if standby_window(clock()) is None:
                    continue
                server = health_factory(("0.0.0.0", port), StandbyHandler)
                health_thread = threading.Thread(target=server.serve_forever, daemon=True)
                health_thread.start()
                log("overnight standby active; gateway unavailable until 05:45 Algeria time")
            if child is not None and not draining:
                if wake is None:
                    closing_file.unlink(missing_ok=True)
                    closed_at = None
                elif ready_file is not None and ready_file.is_file() and monotonic() >= next_probe:
                    next_probe = monotonic() + 60
                    idle, reason = idle_check(directory, state, now, wake)
                    if idle and closed_at is None:
                        closing_file.touch(mode=0o600)
                        closed_at = now
                        reason = "new requests held while existing work drains"
                    elif idle and (now - closed_at).total_seconds() >= QUIET_SECONDS:
                        log("idle verified; requesting graceful gateway shutdown")
                        audit(state, "planned_stop", "overnight_standby", True, now)
                        child.send_signal(signal.SIGTERM)
                        draining = True
                    elif not idle:
                        closing_file.unlink(missing_ok=True)
                        closed_at = None
                    if reason != last_reason:
                        log(reason)
                        last_reason = reason
            wait(1)
        if child is not None and child.poll() is None:
            audit(state, "planned_stop", "platform_signal", True, clock())
            child.send_signal(signal.SIGTERM)
            # Never force-kill in-flight requests to reach a cost target.
            child.wait()
            audit(state, "gateway_stopped", "platform_signal", True, clock())
        return 0
    finally:
        close_health()
        closing_file.unlink(missing_ok=True)
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("gateway command required")
    if os.environ.get("GENAIE_OVERNIGHT_STANDBY") != "1":
        os.execvp(command[0], command)
    directory = Path(os.environ["GENAIE_STANDBY_RUNTIME"])
    state = Path(os.environ.get("OPENCLAW_STATE_DIR", "/home/joseph/.openclaw"))
    return supervise(command, directory, state, args.port, ready_file=directory / "release-ready")


if __name__ == "__main__":
    raise SystemExit(main())
