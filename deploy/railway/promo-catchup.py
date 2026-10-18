#!/usr/bin/env python3
"""Boot-time catch-up for promo crons whose slot passed while the gateway was down.

The seven promo crons (deploy/promo-crons.json) live in the gateway's cron state
and fire once a day at a fixed Africa/Harare minute. Nothing re-runs a slot the
gateway missed: on 2026-10-18 the gateway crash-looped from ~10:09 to 17:18 UTC
(fixed by #125) and that day's 10:35 .. 17:06 ads simply never went out.

The Railway entrypoint starts this script in the background just before it execs
the gateway. It:

1. exits at once when disabled (PROMO_CATCHUP=0), when Harare local time is past
   the posting cutoff (PROMO_CATCHUP_CUTOFF, default 20:00) or when no promo's
   slot has arrived yet today;
2. waits for the gateway to accept connections on its port and answer
   ``openclaw cron list`` as a gateway client, then a settle delay
   (PROMO_CATCHUP_SETTLE_SECONDS, default 120) so WhatsApp is linked;
3. runs, one at a time and in schedule order, every promo whose slot has already
   passed today, exactly as its cron does: ``python3 <script>`` in the manifest's
   commandCwd with the gateway's own environment, so every send goes through
   promo_broadcast (gateway client, per-group "sent today" state, membership
   caps, send spacing and start jitter, and the per-promo run lock).

A promo already delivered today just prints NO_REPLY (logged as one quiet line).
A promo whose slot has not arrived is left to its cron. A promo whose cron is
disabled or missing in the live cron state is skipped. Each run is killed at the
cutoff (or the manifest's job timeout, whichever is first) so nothing is sent
outside the posting window. Never fatal: every failure is logged with the
``[promo-catchup]`` prefix and the script exits 0.

Std-lib only.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

PREFIX = "[promo-catchup]"
DEFAULT_MANIFEST = "/usr/local/share/genaebot/promo-crons.json"
REPO_MANIFEST = Path(__file__).resolve().parent.parent / "promo-crons.json"
DEFAULT_CUTOFF = "20:00"
DEFAULT_SETTLE_SECONDS = 120.0
DEFAULT_READY_TIMEOUT_SECONDS = 900.0
READY_POLL_SECONDS = 5.0
KILL_GRACE_SECONDS = 15.0
# Zones without DST that the promos use, for an image with no tzdata installed.
FIXED_OFFSETS = {"Africa/Harare": timedelta(hours=2)}
DISABLED_VALUES = {"0", "false", "no", "off", "disabled"}


def log(message: str) -> None:
    print(f"{PREFIX} {message}", flush=True)


# ------------------------------------------------------------------ planning ----
def parse_daily_cron(expr: str) -> Optional[tuple[int, int]]:
    """'40 8 * * *' -> (8, 40). Anything that is not a plain once-a-day
    schedule returns None (the catch-up then leaves that promo alone)."""
    parts = str(expr or "").split()
    if len(parts) != 5 or parts[2:] != ["*", "*", "*"]:
        return None
    minute, hour = parts[0], parts[1]
    if not (minute.isdigit() and hour.isdigit()):
        return None
    h, m = int(hour), int(minute)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return (h, m)


def parse_hhmm(value: str) -> Optional[tuple[int, int]]:
    try:
        hour, minute = str(value).strip().split(":")
        h, m = int(hour), int(minute)
    except (ValueError, AttributeError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return (h, m)


def zone(tz_name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(tz_name)
    except Exception:
        offset = FIXED_OFFSETS.get(tz_name)
        return timezone(offset, tz_name) if offset is not None else None


def local_now(tz_name: str, now_utc: Optional[datetime] = None) -> Optional[datetime]:
    tz = zone(tz_name)
    if tz is None:
        return None
    return (now_utc or datetime.now(timezone.utc)).astimezone(tz)


def plan(manifest: dict, now_utc: datetime, cutoff: tuple[int, int]):
    """Split the manifest's promos for "today" in each promo's timezone.

    Returns ``(due, later, skipped, window)``: ``due`` is ``[(slot, promo)]`` in
    schedule order for promos whose slot has passed, ``later`` the same for slots
    still to come today, ``skipped`` ``[(promo, reason)]`` and ``window`` None when
    inside the posting window, else why the whole catch-up stands down."""
    due, later, skipped = [], [], []
    past_cutoff = False
    for promo in manifest.get("promos") or []:
        slot = parse_daily_cron(promo.get("cron", ""))
        if slot is None:
            skipped.append((promo, "schedule %r is not a plain daily time" % promo.get("cron")))
            continue
        now = local_now(promo.get("tz") or "Africa/Harare", now_utc)
        if now is None:
            skipped.append((promo, "unknown timezone %r" % promo.get("tz")))
            continue
        if (now.hour, now.minute) >= cutoff:
            past_cutoff = True
            continue
        entry = ("%02d:%02d" % slot, promo)
        (due if (now.hour, now.minute) >= slot else later).append(entry)
    due.sort(key=lambda e: e[0])
    later.sort(key=lambda e: e[0])
    window = None
    if past_cutoff and not due and not later:
        window = "past the %02d:%02d posting cutoff" % cutoff
    elif not due:
        window = "no promo slot has passed yet today"
    return due, later, skipped, window


def seconds_until_cutoff(tz_name: str, cutoff: tuple[int, int],
                         now_utc: Optional[datetime] = None) -> float:
    now = local_now(tz_name, now_utc)
    if now is None:
        return 0.0
    end = now.replace(hour=cutoff[0], minute=cutoff[1], second=0, microsecond=0)
    return max(0.0, (end - now).total_seconds())


# ----------------------------------------------------------------- readiness ----
def port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def gateway_env() -> dict:
    """Same environment promo_broadcast.gateway_env builds for its own calls."""
    env = dict(os.environ)
    env.pop("OPENCLAW_HOME", None)
    token = os.environ.get("GATEWAY_AUTH_TOKEN")
    if token:
        env["OPENCLAW_GATEWAY_TOKEN"] = token
    return env


def read_cron_jobs() -> Optional[list]:
    """Live cron jobs via the gateway client, or None when that fails."""
    try:
        p = subprocess.run(["openclaw", "cron", "list", "--all", "--json"],
                           capture_output=True, text=True, timeout=60, env=gateway_env())
    except Exception:
        return None
    if p.returncode != 0:
        return None
    out = p.stdout or ""
    start = out.find("{")
    try:
        jobs = (json.loads(out[start:]) if start >= 0 else {}).get("jobs")
    except ValueError:
        return None
    return jobs if isinstance(jobs, list) else None


def wait_for_gateway(port: int, timeout: float, now: Callable[[], float] = time.monotonic,
                     sleep: Callable[[float], None] = time.sleep,
                     probe_port: Callable[[int], bool] = port_open,
                     probe_cron: Callable[[], Optional[list]] = read_cron_jobs):
    """(ready, jobs): wait until the port accepts connections AND the gateway
    answers ``cron list`` as a client (which is how every promo send connects)."""
    deadline = now() + timeout
    while True:
        if probe_port(port):
            jobs = probe_cron()
            if jobs is not None:
                return True, jobs
        if now() >= deadline:
            return False, None
        sleep(READY_POLL_SECONDS)


# ------------------------------------------------------------------- running ----
def run_promo(script: str, cwd: str, timeout: float, name: str) -> tuple[str, int]:
    """Run one promo script the way its cron does, streaming its output. Returns
    ``(outcome, returncode)`` with outcome "ok", "nothing-pending", "failed" or
    "killed-at-cutoff"."""
    proc = subprocess.Popen(["python3", script], cwd=cwd, env=dict(os.environ),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True)
    deadline = time.monotonic() + timeout
    lines = []
    killed = False

    def _kill() -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _watchdog() -> None:
        nonlocal killed
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                killed = True
                _kill()
                return
            time.sleep(1)

    threading.Thread(target=_watchdog, daemon=True).start()
    assert proc.stdout is not None
    with proc.stdout:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            lines.append(line)
            if line.strip() and line.strip() != "NO_REPLY":
                log(f"{name}: {line}")
    rc = proc.wait()
    if killed:
        return "killed-at-cutoff", rc
    if rc == 0 and [l.strip() for l in lines if l.strip()][-1:] == ["NO_REPLY"]:
        return "nothing-pending", rc
    return ("ok" if rc == 0 else "failed"), rc


def env_float(env: dict, name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log(f"ignoring non-numeric {name}={raw!r}")
        return default
    return value if value >= 0 else default


def load_manifest(path: Optional[str]) -> dict:
    candidates = [Path(path)] if path else [Path(DEFAULT_MANIFEST), REPO_MANIFEST]
    for candidate in candidates:
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise FileNotFoundError("promo manifest not found: %s" % ", ".join(map(str, candidates)))


def catch_up(manifest: dict, env: dict, port: int,
             utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
             sleep: Callable[[float], None] = time.sleep,
             wait: Callable[..., tuple] = wait_for_gateway,
             runner: Callable[[str, str, float, str], tuple] = run_promo) -> int:
    """The whole catch-up; returns the number of promos it ran."""
    if (env.get("PROMO_CATCHUP") or "1").strip().lower() in DISABLED_VALUES:
        log("disabled (PROMO_CATCHUP=%s); not checking for missed promos" % env.get("PROMO_CATCHUP"))
        return 0
    cutoff_raw = env.get("PROMO_CATCHUP_CUTOFF") or DEFAULT_CUTOFF
    cutoff = parse_hhmm(cutoff_raw)
    if cutoff is None:
        log(f"ignoring invalid PROMO_CATCHUP_CUTOFF={cutoff_raw!r}; using {DEFAULT_CUTOFF}")
        cutoff = parse_hhmm(DEFAULT_CUTOFF)
    assert cutoff is not None

    # Cheap pre-check before waiting on the gateway at all (e.g. a night-time boot).
    _, _, _, window = plan(manifest, utcnow(), cutoff)
    if window:
        log(f"nothing to catch up: {window}")
        return 0

    ready, jobs = wait(port, env_float(env, "PROMO_CATCHUP_READY_TIMEOUT_SECONDS",
                                       DEFAULT_READY_TIMEOUT_SECONDS))
    if not ready:
        log("gateway did not become ready; skipping catch-up (the next slots fire normally)")
        return 0
    settle = env_float(env, "PROMO_CATCHUP_SETTLE_SECONDS", DEFAULT_SETTLE_SECONDS)
    if settle > 0:
        log("gateway is up; waiting %.0fs for WhatsApp to settle" % settle)
        sleep(settle)

    # Decide once, after settling: a slot that arrives from here on is its cron's.
    due, later, skipped, window = plan(manifest, utcnow(), cutoff)
    for promo, reason in skipped:
        log(f"skip {promo.get('name')} ({reason})")
    for slot, promo in later:
        log(f"skip {promo.get('name')} (scheduled {slot}, not yet due; its cron will fire)")
    if window:
        log(f"nothing to catch up: {window}")
        return 0

    live = {}
    for job in jobs or []:
        if isinstance(job, dict) and job.get("name"):
            live[job["name"]] = job
    timeout_default = float(manifest.get("timeoutSeconds") or 7200)
    cwd = manifest.get("commandCwd") or "/home/joseph/.openclaw/workspace"

    ran = 0
    for slot, promo in due:
        name = promo.get("name") or promo.get("slug")
        tz_name = promo.get("tz") or "Africa/Harare"
        if live and name not in live:
            log(f"skip {name} (scheduled {slot}, but no such cron in gateway state)")
            continue
        if live and live[name].get("enabled") is False:
            log(f"skip {name} (scheduled {slot}, but its cron is disabled)")
            continue
        remaining = seconds_until_cutoff(tz_name, cutoff, utcnow())
        if remaining <= 0:
            log("stopping: reached the %02d:%02d posting cutoff" % cutoff)
            break
        log(f"running {name} (scheduled {slot}, missed)")
        try:
            outcome, rc = runner(promo["script"], cwd, min(timeout_default, remaining), name)
        except Exception as exc:  # one promo's failure must not stop the rest
            log(f"{name}: could not run ({type(exc).__name__}: {exc})")
            continue
        ran += 1
        if outcome == "nothing-pending":
            log(f"{name}: nothing pending (already sent today)")
        elif outcome == "killed-at-cutoff":
            log(f"{name}: stopped at the posting cutoff / job timeout")
        elif outcome == "failed":
            log(f"{name}: exited {rc}")
        else:
            log(f"{name}: finished")
    log(f"done: {ran} promos")
    return ran


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", default=os.environ.get("PROMO_CATCHUP_MANIFEST"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("OPENCLAW_GATEWAY_PORT") or 18789))
    args = parser.parse_args(argv)
    try:
        catch_up(load_manifest(args.manifest), dict(os.environ), args.port)
    except Exception as exc:  # never let the catch-up surface as a failure
        log(f"failed: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
