"""Tests for the boot-time promo catch-up (deploy/railway/promo-catchup.py) and the
per-promo run lock in workspace/lib/promo_broadcast.py that keeps a catch-up run
and a scheduled cron run of the same promo from double-posting.

Nothing here contacts a gateway: the catch-up's readiness wait and promo runner
are injected, and the lock tests stub the WhatsApp send.
"""

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy/railway/promo-catchup.py"
ENTRYPOINT = ROOT / "deploy/railway/entrypoint.sh"
DOCKERFILE = ROOT / "deploy/railway/Dockerfile"
MANIFEST = json.loads((ROOT / "deploy/promo-crons.json").read_text(encoding="utf-8"))

SPEC = importlib.util.spec_from_file_location("promo_catchup", SCRIPT)
CATCHUP = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(CATCHUP)

sys.path.insert(0, str(ROOT / "workspace/lib"))
import promo_broadcast as pb  # noqa: E402

ALL_IN_ORDER = [
    "borehole-drilling-promo", "fruit-trees-promo", "appliance-repairs-promo",
    "cv-promo", "building-materials-promo", "genaie-promo", "genaie-leads-promo",
]


def utc(hh, mm):
    """2026-10-18 at hh:mm UTC (Africa/Harare is UTC+2, no DST)."""
    return datetime(2026, 10, 18, hh, mm, tzinfo=timezone.utc)


def live_jobs(**overrides):
    jobs = []
    for promo in MANIFEST["promos"]:
        job = {"name": promo["name"], "enabled": True}
        job.update(overrides.get(promo["name"], {}))
        jobs.append(job)
    return jobs


class Harness:
    """Drives catch_up with a fake clock, readiness wait and runner."""

    def __init__(self, now, env=None, ready=True, jobs=None, outcome=("ok", 0)):
        self.now = now
        self.env = {"PROMO_CATCHUP_SETTLE_SECONDS": "0"}
        self.env.update(env or {})
        self.ready = ready
        self.jobs = live_jobs() if jobs is None else jobs
        self.outcome = outcome
        self.waited = 0
        self.runs = []
        self.sleeps = []

    def wait(self, port, timeout):
        self.waited += 1
        return (self.ready, self.jobs if self.ready else None)

    def runner(self, script, cwd, timeout, name):
        self.runs.append((name, script, cwd, timeout))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def go(self):
        out = io.StringIO()
        with redirect_stdout(out):
            ran = CATCHUP.catch_up(MANIFEST, self.env, 18789, utcnow=lambda: self.now,
                                   sleep=self.sleeps.append, wait=self.wait,
                                   runner=self.runner)
        self.log = out.getvalue()
        return ran

    @property
    def names(self):
        return [r[0] for r in self.runs]


class ScheduleParsingTests(unittest.TestCase):
    def test_daily_cron_expressions(self):
        self.assertEqual(CATCHUP.parse_daily_cron("40 8 * * *"), (8, 40))
        self.assertEqual(CATCHUP.parse_daily_cron("6 17 * * *"), (17, 6))
        for bad in ("*/5 * * * *", "0 8 * * 1-5", "0 8,9 * * *", "61 8 * * *", "", "0 8 * *"):
            self.assertIsNone(CATCHUP.parse_daily_cron(bad), bad)

    def test_every_manifest_promo_is_understood(self):
        for promo in MANIFEST["promos"]:
            self.assertIsNotNone(CATCHUP.parse_daily_cron(promo["cron"]), promo["name"])

    def test_cutoff_parsing(self):
        self.assertEqual(CATCHUP.parse_hhmm("20:00"), (20, 0))
        self.assertEqual(CATCHUP.parse_hhmm(" 9:30 "), (9, 30))
        for bad in ("24:00", "20", "x:y", ""):
            self.assertIsNone(CATCHUP.parse_hhmm(bad), bad)

    def test_harare_fallback_without_tzdata(self):
        self.assertEqual(CATCHUP.FIXED_OFFSETS["Africa/Harare"].total_seconds(), 7200)
        self.assertEqual(CATCHUP.local_now("Africa/Harare", utc(10, 0)).hour, 12)


class PlanTests(unittest.TestCase):
    def names(self, entries):
        return [p["name"] for _, p in entries]

    def test_mid_day_splits_due_from_later_in_schedule_order(self):
        due, later, skipped, window = CATCHUP.plan(MANIFEST, utc(10, 0), (20, 0))  # 12:00 CAT
        self.assertIsNone(window)
        self.assertEqual(self.names(due), ALL_IN_ORDER[:4])
        self.assertEqual([s for s, _ in due], ["08:13", "08:40", "10:35", "11:39"])
        self.assertEqual(self.names(later), ALL_IN_ORDER[4:])
        self.assertEqual(skipped, [])

    def test_the_2026_10_18_recovery_time_has_every_promo_due(self):
        due, later, _, window = CATCHUP.plan(MANIFEST, utc(17, 30), (20, 0))  # 19:30 CAT
        self.assertIsNone(window)
        self.assertEqual(self.names(due), ALL_IN_ORDER)
        self.assertEqual(later, [])

    def test_slot_minute_itself_counts_as_due(self):
        due, _, _, _ = CATCHUP.plan(MANIFEST, utc(6, 13), (20, 0))  # 08:13 CAT
        self.assertEqual(self.names(due), ["borehole-drilling-promo"])

    def test_before_the_first_promo_nothing_is_due(self):
        due, later, _, window = CATCHUP.plan(MANIFEST, utc(6, 12), (20, 0))  # 08:12 CAT
        self.assertEqual(due, [])
        self.assertEqual(len(later), 7)
        self.assertIn("no promo slot has passed", window)

    def test_past_the_cutoff_nothing_runs(self):
        due, later, _, window = CATCHUP.plan(MANIFEST, utc(18, 0), (20, 0))  # 20:00 CAT
        self.assertEqual((due, later), ([], []))
        self.assertIn("cutoff", window)

    def test_manifest_order_does_not_matter(self):
        shuffled = dict(MANIFEST, promos=list(reversed(MANIFEST["promos"])))
        due, _, _, _ = CATCHUP.plan(shuffled, utc(17, 30), (20, 0))
        self.assertEqual(self.names(due), ALL_IN_ORDER)

    def test_unsupported_schedule_and_zone_are_skipped(self):
        manifest = {"promos": [dict(MANIFEST["promos"][0], cron="*/5 * * * *"),
                               dict(MANIFEST["promos"][1], tz="Mars/Olympus")]}
        due, later, skipped, _ = CATCHUP.plan(manifest, utc(17, 30), (20, 0))
        self.assertEqual((due, later), ([], []))
        self.assertEqual(len(skipped), 2)


class CatchUpTests(unittest.TestCase):
    def test_runs_missed_promos_in_order_like_the_cron(self):
        h = Harness(utc(17, 30))  # 19:30 CAT
        self.assertEqual(h.go(), 7)
        self.assertEqual(h.names, ALL_IN_ORDER)
        for name, script, cwd, timeout in h.runs:
            promo = next(p for p in MANIFEST["promos"] if p["name"] == name)
            self.assertEqual(script, promo["script"])
            self.assertEqual(cwd, MANIFEST["commandCwd"])
            self.assertLessEqual(timeout, 30 * 60)  # killed at the 20:00 cutoff
        self.assertIn("[promo-catchup] running fruit-trees-promo (scheduled 08:40, missed)", h.log)
        self.assertIn("[promo-catchup] done: 7 promos", h.log)

    def test_not_yet_due_promos_are_left_to_their_cron(self):
        h = Harness(utc(10, 0))  # 12:00 CAT
        h.go()
        self.assertEqual(h.names, ALL_IN_ORDER[:4])
        self.assertIn("skip genaie-promo (scheduled 14:26, not yet due", h.log)

    def test_run_timeout_is_the_job_timeout_far_from_the_cutoff(self):
        h = Harness(utc(7, 0))  # 09:00 CAT
        h.go()
        self.assertEqual(h.names, ALL_IN_ORDER[:2])
        self.assertEqual(h.runs[0][3], float(MANIFEST["timeoutSeconds"]))

    def test_disabled_flag_does_nothing(self):
        for value in ("0", "false", "off"):
            h = Harness(utc(17, 30), env={"PROMO_CATCHUP": value})
            self.assertEqual(h.go(), 0)
            self.assertEqual((h.waited, h.runs), (0, []))
            self.assertIn("disabled", h.log)

    def test_past_cutoff_does_not_even_wait_for_the_gateway(self):
        h = Harness(utc(18, 30))  # 20:30 CAT
        self.assertEqual(h.go(), 0)
        self.assertEqual((h.waited, h.runs), (0, []))
        self.assertIn("cutoff", h.log)

    def test_before_first_promo_does_not_wait(self):
        h = Harness(utc(5, 0))  # 07:00 CAT
        self.assertEqual(h.go(), 0)
        self.assertEqual(h.waited, 0)

    def test_cutoff_is_env_overridable(self):
        h = Harness(utc(18, 30), env={"PROMO_CATCHUP_CUTOFF": "21:00"})
        self.assertEqual(h.go(), 7)
        h = Harness(utc(10, 0), env={"PROMO_CATCHUP_CUTOFF": "11:00"})
        self.assertEqual(h.go(), 0)
        h = Harness(utc(10, 0), env={"PROMO_CATCHUP_CUTOFF": "nonsense"})
        self.assertEqual(h.go(), 4)  # falls back to 20:00

    def test_gateway_never_ready_means_no_runs(self):
        h = Harness(utc(17, 30), ready=False)
        self.assertEqual(h.go(), 0)
        self.assertEqual(h.runs, [])
        self.assertIn("did not become ready", h.log)

    def test_settle_delay_is_taken_after_readiness(self):
        h = Harness(utc(17, 30), env={"PROMO_CATCHUP_SETTLE_SECONDS": "90"})
        h.go()
        self.assertEqual(h.sleeps, [90.0])

    def test_disabled_or_missing_live_cron_is_skipped(self):
        jobs = [j for j in live_jobs(**{"cv-promo": {"enabled": False}})
                if j["name"] != "genaie-promo"]
        h = Harness(utc(17, 30), jobs=jobs)
        h.go()
        self.assertNotIn("cv-promo", h.names)
        self.assertNotIn("genaie-promo", h.names)
        self.assertEqual(len(h.names), 5)
        self.assertIn("skip cv-promo (scheduled 11:39, but its cron is disabled)", h.log)

    def test_already_sent_is_one_quiet_line_per_promo(self):
        h = Harness(utc(17, 30), outcome=("nothing-pending", 0))
        h.go()
        self.assertEqual(h.log.count("nothing pending (already sent today)"), 7)

    def test_a_failing_promo_does_not_stop_the_rest(self):
        h = Harness(utc(17, 30), outcome=RuntimeError("boom"))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(h.go(), 0)
        self.assertEqual(len(h.runs), 7)
        self.assertIn("could not run (RuntimeError: boom)", h.log)

    def test_main_never_raises_and_exits_zero(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = CATCHUP.main(["--manifest", "/nonexistent/promo-crons.json"])
        self.assertEqual(rc, 0)
        self.assertIn("[promo-catchup] failed:", out.getvalue())


class ReadinessTests(unittest.TestCase):
    def test_waits_for_port_then_gateway_client(self):
        t = [0.0]
        port_states = iter([False, False, True, True])
        cron_states = iter([None, [{"name": "x"}]])
        ready, jobs = CATCHUP.wait_for_gateway(
            1, 600, now=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s),
            probe_port=lambda p: next(port_states), probe_cron=lambda: next(cron_states))
        self.assertTrue(ready)
        self.assertEqual(jobs, [{"name": "x"}])

    def test_gives_up_at_the_timeout(self):
        t = [0.0]
        ready, _ = CATCHUP.wait_for_gateway(
            1, 30, now=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s),
            probe_port=lambda p: False, probe_cron=lambda: [])
        self.assertFalse(ready)
        self.assertLessEqual(t[0], 30 + CATCHUP.READY_POLL_SECONDS)


class RunPromoTests(unittest.TestCase):
    def script(self, body):
        d = tempfile.mkdtemp()
        path = Path(d) / "post_ad.py"
        path.write_text(textwrap.dedent(body))
        return str(path), d

    def run_quiet(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            result = CATCHUP.run_promo(*args)
        return result, out.getvalue()

    def test_no_reply_is_nothing_pending_and_silent(self):
        path, d = self.script("print('NO_REPLY')\n")
        (outcome, rc), log = self.run_quiet(path, d, 30, "x-promo")
        self.assertEqual((outcome, rc), ("nothing-pending", 0))
        self.assertEqual(log, "")

    def test_runs_in_cwd_and_streams_output(self):
        path, d = self.script("import os\nprint('SENT 2/2 in', os.getcwd())\n")
        (outcome, rc), log = self.run_quiet(path, d, 30, "x-promo")
        self.assertEqual(outcome, "ok")
        self.assertIn("[promo-catchup] x-promo: SENT 2/2 in %s" % os.path.realpath(d),
                      log.replace(d, os.path.realpath(d)))

    def test_failure_exit_is_reported(self):
        path, d = self.script("import sys\nsys.exit(1)\n")
        (outcome, rc), _ = self.run_quiet(path, d, 30, "x-promo")
        self.assertEqual((outcome, rc), ("failed", 1))

    def test_killed_at_the_deadline(self):
        path, d = self.script("import time\nprint('waiting', flush=True)\ntime.sleep(60)\n")
        start = time.monotonic()
        (outcome, _), _ = self.run_quiet(path, d, 1, "x-promo")
        self.assertEqual(outcome, "killed-at-cutoff")
        self.assertLess(time.monotonic() - start, 20)


class RunLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.image = self.tmp / "ad.png"
        self.image.write_bytes(b"img")
        self.targets = self.tmp / "targets.json"
        self.targets.write_text(json.dumps({"groups": ["1@g.us", "2@g.us"]}))
        self.state = self.tmp / "state" / "state.json"
        self.sent = []
        self._orig_send = pb.send_ad
        pb.send_ad = lambda target, text, image: (self.sent.append(target) or
                                                  (True, '{"messageId":"m"}'))

    def tearDown(self):
        pb.send_ad = self._orig_send

    def broadcast(self, argv=()):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = pb.broadcast(text="hi", image=str(self.image), targets_file=str(self.targets),
                              state_file=str(self.state), send_delay=0, argv=list(argv),
                              start_jitter=0, cron_timeout=None, caps=None)
        return rc, out.getvalue(), err.getvalue()

    def test_lock_file_sits_next_to_the_state_file(self):
        self.assertEqual(pb.lock_path("/x/y/state.json"), "/x/y/state.json.lock")

    def test_held_lock_makes_a_second_run_exit_cleanly_without_sending(self):
        ok, fd = pb.acquire_run_lock(str(self.state))
        self.assertTrue(ok)
        try:
            rc, out, err = self.broadcast()
        finally:
            pb.release_run_lock(fd)
        self.assertEqual(rc, 0)
        self.assertEqual(self.sent, [])
        self.assertIn("already running", err)
        self.assertEqual(out.strip(), "NO_REPLY")
        self.assertFalse(self.state.exists())  # state untouched
        rc, _, _ = self.broadcast()  # lock released -> sends normally
        self.assertEqual((rc, self.sent), (0, ["1@g.us", "2@g.us"]))

    def test_lock_held_by_another_process_blocks_a_concurrent_run(self):
        holder = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent("""
                import sys, time
                sys.path.insert(0, sys.argv[1])
                import promo_broadcast as pb
                ok, fd = pb.acquire_run_lock(sys.argv[2])
                print("held" if ok else "busy", flush=True)
                time.sleep(30)
            """), str(ROOT / "workspace/lib"), str(self.state)],
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            rc, _, err = self.broadcast()
            self.assertEqual((rc, self.sent), (0, []))
            self.assertIn("already running", err)
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()
        rc, _, _ = self.broadcast()  # the holder died -> the kernel dropped its lock
        self.assertEqual(self.sent, ["1@g.us", "2@g.us"])

    def test_lock_is_released_after_a_run_even_on_error(self):
        self.image.unlink()  # broadcast returns 1 (missing image)
        rc, _, _ = self.broadcast()
        self.assertEqual(rc, 1)
        ok, fd = pb.acquire_run_lock(str(self.state))
        self.assertTrue(ok)
        pb.release_run_lock(fd)

    def test_dry_run_ignores_the_lock(self):
        ok, fd = pb.acquire_run_lock(str(self.state))
        try:
            rc, out, err = self.broadcast(["--dry-run"])
        finally:
            pb.release_run_lock(fd)
        self.assertEqual(rc, 0)
        self.assertIn("[dry-run]", out)
        self.assertNotIn("already running", err)

    def test_second_run_after_completion_is_no_reply(self):
        self.broadcast()
        rc, out, _ = self.broadcast()
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "NO_REPLY")
        self.assertEqual(self.sent, ["1@g.us", "2@g.us"])


class EntrypointWiringTests(unittest.TestCase):
    def setUp(self):
        self.text = ENTRYPOINT.read_text(encoding="utf-8")

    def block(self):
        start = self.text.index('if [ "${PROMO_CATCHUP:-1}" != "0" ]; then')
        end = self.text.index("\nfi\n", start) + len("\nfi\n")
        return self.text[start:end]

    def test_started_after_the_lease_wait_and_before_the_gateway(self):
        at = self.text.index("promo-catchup.py")
        self.assertLess(self.text.index("wait-gateway-lease.py \\"), at)
        self.assertLess(at, self.text.index("exec runuser -u joseph -- openclaw gateway"))

    def test_backgrounded_as_joseph_and_non_fatal(self):
        block = self.block()
        self.assertIn("runuser -u joseph -- python3 -u /usr/local/bin/promo-catchup.py", block)
        self.assertIn("--manifest /usr/local/share/genaebot/promo-crons.json", block)
        self.assertRegex(block, r"\) </dev/null &\n")
        self.assertIn("|| echo", block)
        self.assertIn("PROMO_CATCHUP=0", block)

    def run_block(self, env_extra, runuser_body):
        """Run the entrypoint's catch-up block, then a stand-in for the gateway
        exec. Returns (lines, seconds until the gateway line, exit code)."""
        script = (
            "set -euo pipefail\n"
            "log() { echo \"[entrypoint] $*\"; }\n"
            "runuser() { %s; }\n"
            "PORT=18789\n%s\necho gateway-would-start\n" % (runuser_body, self.block())
        )
        env = dict(os.environ)
        env.pop("PROMO_CATCHUP", None)
        env.update(env_extra)
        start = time.monotonic()
        p = subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, env=env)
        lines, gateway_at = [], None
        with p.stdout:
            for line in p.stdout:  # EOF only once the background step exits too
                lines.append(line.rstrip("\n"))
                if line.strip() == "gateway-would-start":
                    gateway_at = time.monotonic() - start
        return lines, gateway_at, p.wait(timeout=30)

    def test_a_slow_failing_catchup_neither_blocks_nor_fails_the_boot(self):
        lines, gateway_at, rc = self.run_block({}, "sleep 3; return 7")
        self.assertEqual(rc, 0, lines)
        self.assertIsNotNone(gateway_at, lines)
        self.assertLess(gateway_at, 2.5)  # did not wait for the 3 s catch-up
        self.assertTrue(any("promo catch-up started in the background" in l for l in lines))
        # Its failure is only logged, after the gateway has started.
        self.assertGreater(
            lines.index("[promo-catchup] exited abnormally; the gateway is unaffected"),
            lines.index("gateway-would-start"))

    def test_disabled_skips_the_background_step(self):
        lines, _, rc = self.run_block({"PROMO_CATCHUP": "0"}, "echo SHOULD-NOT-RUN")
        self.assertEqual(rc, 0)
        self.assertNotIn("SHOULD-NOT-RUN", lines)
        self.assertIn("[entrypoint] promo catch-up disabled (PROMO_CATCHUP=0)", lines)

    def test_dockerfile_ships_the_script_and_manifest(self):
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("COPY deploy/railway/promo-catchup.py /usr/local/bin/promo-catchup.py", dockerfile)
        self.assertIn("COPY deploy/promo-crons.json /usr/local/share/genaebot/promo-crons.json", dockerfile)
        self.assertRegex(dockerfile, r"chmod 0755[^\n]*\n?[^\n]*/usr/local/bin/promo-catchup\.py")

    def test_catchup_runs_scripts_the_cron_registers(self):
        for promo in MANIFEST["promos"]:
            rel = promo["script"].replace(MANIFEST["commandCwd"] + "/", "workspace/")
            self.assertTrue((ROOT / rel).is_file(), rel)

    def test_script_is_executable(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main()
