"""Random spacing between group sends in the shared promo engine
(workspace/lib/promo_broadcast.py).

A fixed gap made the promo crons post at a metronome-regular cadence, which
WhatsApp spam detection reportedly flags. The engine now sleeps a uniform random
20-60 s between send attempts. These tests pin that: every gap is within the
bounds, there is no sleep before the first or after the last send, and nothing
sleeps for groups that are not sent to or on --dry-run / --refs.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes them.
"""

import io
import json
import os
import random
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = ROOT / "workspace/lib"
CRON = ROOT / "workspace/cron"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))
import promo_broadcast as pb  # noqa: E402
import json_store  # noqa: E402

OK = (True, '{"messageId":"m"}')


class RecordingRng:
    """Real uniform draws (seeded) that also record the bounds asked for."""

    def __init__(self, seed=0):
        self._rng = random.Random(seed)
        self.calls = []

    def uniform(self, a, b):
        self.calls.append((a, b))
        return self._rng.uniform(a, b)


class SendSpacingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.image = self.tmp / "ad.png"
        self.image.write_bytes(b"img")
        self.state = self.tmp / "state.json"
        self.events = []  # ordered ("send", jid) / ("sleep", seconds)
        self._saved_send = pb.send_ad
        self._saved_env = {k: os.environ.pop(k, None)
                           for k in ("PROMO_SEND_DELAY_MIN", "PROMO_SEND_DELAY_MAX")}
        pb.send_ad = self._fake_send
        self.send_result = lambda jid: OK

    def tearDown(self):
        pb.send_ad = self._saved_send
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _fake_send(self, target, text, image):
        self.events.append(("send", target))
        return self.send_result(target)

    def _sleep(self, seconds):
        self.events.append(("sleep", seconds))

    def _run(self, groups, argv=(), weekly=None, **kwargs):
        data = {"groups": groups}
        if weekly is not None:
            data["weekly_groups"] = weekly
        targets = self.tmp / "targets.json"
        targets.write_text(json.dumps(data))
        kwargs.setdefault("rng", RecordingRng())
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return pb.broadcast(text="hi", image=str(self.image),
                                targets_file=str(targets), state_file=str(self.state),
                                argv=list(argv), sleep=self._sleep, **kwargs)

    def sleeps(self):
        return [v for kind, v in self.events if kind == "sleep"]

    def kinds(self):
        return [kind for kind, _ in self.events]

    # ------------------------------------------------------------ defaults ----
    def test_default_gaps_are_uniform_20_to_60_between_sends_only(self):
        groups = ["%d@g.us" % i for i in range(25)]
        rng = RecordingRng(seed=42)
        # The budget the installer passes with its 3600 s timeout: ample room.
        self.assertEqual(self._run(groups, argv=["--run-budget", "3540"], rng=rng), 0)
        # send, sleep, send, ..., sleep, send: no leading or trailing sleep.
        self.assertEqual(self.kinds(),
                         ["send"] + ["sleep", "send"] * (len(groups) - 1))
        self.assertEqual(rng.calls, [(20.0, 60.0)] * (len(groups) - 1))
        for gap in self.sleeps():
            self.assertGreaterEqual(gap, 20.0)
            self.assertLessEqual(gap, 60.0)
        self.assertGreater(len(set(self.sleeps())), 1, "gaps must not be constant")

    def test_uses_random_module_when_no_rng_is_injected(self):
        saved = pb.random
        calls = []
        pb.random = type("R", (), {"uniform": staticmethod(
            lambda a, b: calls.append((a, b)) or 33.0)})
        try:
            self._run(["1@g.us", "2@g.us"], rng=None)
        finally:
            pb.random = saved
        self.assertEqual(calls, [(20.0, 60.0)])
        self.assertEqual(self.sleeps(), [33.0])

    def test_custom_bounds(self):
        rng = RecordingRng()
        self._run(["1@g.us", "2@g.us", "3@g.us"], rng=rng,
                  send_delay_min=5, send_delay_max=7)
        self.assertEqual(rng.calls, [(5.0, 7.0)] * 2)
        for gap in self.sleeps():
            self.assertTrue(5.0 <= gap <= 7.0, gap)

    def test_env_overrides_bounds(self):
        os.environ["PROMO_SEND_DELAY_MIN"] = "30"
        os.environ["PROMO_SEND_DELAY_MAX"] = "45"
        rng = RecordingRng()
        self._run(["1@g.us", "2@g.us"], rng=rng)
        self.assertEqual(rng.calls, [(30.0, 45.0)])

    def test_bad_env_falls_back_to_defaults(self):
        os.environ["PROMO_SEND_DELAY_MIN"] = "soon"
        os.environ["PROMO_SEND_DELAY_MAX"] = "-5"
        with redirect_stderr(io.StringIO()):
            self.assertEqual(pb.send_gap_bounds(), (20.0, 60.0))

    def test_inverted_bounds_are_swapped(self):
        self.assertEqual(pb.send_gap_bounds(send_delay_min=60, send_delay_max=20),
                         (20.0, 60.0))

    def test_legacy_fixed_send_delay_pins_both_bounds(self):
        rng = RecordingRng()
        self._run(["1@g.us", "2@g.us"], rng=rng, send_delay=0)
        self.assertEqual(rng.calls, [(0.0, 0.0)])
        self.assertEqual(self.sleeps(), [0.0])

    # -------------------------------------------------------- when to sleep ----
    def test_single_send_never_sleeps(self):
        self._run(["1@g.us"])
        self.assertEqual(self.kinds(), ["send"])

    def test_dry_run_and_refs_never_sleep_or_send(self):
        groups = ["%d@g.us" % i for i in range(5)]
        self.assertEqual(self._run(groups, argv=["--dry-run"]), 0)
        self.assertEqual(self._run(groups, argv=["--refs"]), 0)
        self.assertEqual(self.events, [])

    def test_already_sent_groups_are_not_slept_for(self):
        today = date.today().isoformat()
        json_store.write_json_atomic(str(self.state), {
            "sent": {"1@g.us": today, "3@g.us": today, "5@g.us": today},
            "resolved": {}})
        self._run(["%d@g.us" % i for i in range(1, 6)])
        # Only 2 and 4 are pending: exactly one gap, between them.
        self.assertEqual(self.events[0], ("send", "2@g.us"))
        self.assertEqual(self.kinds(), ["send", "sleep", "send"])
        self.assertEqual(self.events[2], ("send", "4@g.us"))

    def test_all_sent_today_never_sleeps(self):
        today = date.today().isoformat()
        json_store.write_json_atomic(str(self.state), {
            "sent": {"1@g.us": today, "2@g.us": today}, "resolved": {}})
        self._run(["1@g.us", "2@g.us"])
        self.assertEqual(self.events, [])

    def test_weekly_group_off_its_day_is_not_slept_for(self):
        not_today = (date.today().weekday() + 1) % 7
        self._run(["1@g.us", "2@g.us"], weekly={"w@g.us": [not_today]})
        self.assertEqual(self.kinds(), ["send", "sleep", "send"])
        self.assertNotIn(("send", "w@g.us"), self.events)

    # ------------------------------------------------------- idempotency ----
    def test_state_is_committed_before_each_gap(self):
        """Each successful send is on disk before the next sleep, so a run killed
        mid-gap (e.g. by the cron timeout) never re-posts on retry."""
        on_disk_at_sleep = []

        def sleep(seconds):
            on_disk_at_sleep.append(
                sorted(json_store.load_json(str(self.state), {}).get("sent", {})))

        self._sleep = sleep
        self._run(["1@g.us", "2@g.us", "3@g.us"])
        self.assertEqual(on_disk_at_sleep, [["1@g.us"], ["1@g.us", "2@g.us"]])

    def test_rerun_after_interruption_skips_sent_groups(self):
        class Killed(Exception):
            pass

        def sleep(seconds):
            if self.kinds().count("send") == 2:
                raise Killed()
            self.events.append(("sleep", seconds))

        self._sleep = sleep
        with self.assertRaises(Killed):
            self._run(["1@g.us", "2@g.us", "3@g.us"])
        self.events.clear()
        self._sleep = lambda s: self.events.append(("sleep", s))
        self.assertEqual(self._run(["1@g.us", "2@g.us", "3@g.us"]), 0)
        self.assertEqual(self.events, [("send", "3@g.us")])


class CallerWiringTests(unittest.TestCase):
    def test_promo_scripts_do_not_pin_a_fixed_delay(self):
        scripts = sorted(CRON.glob("*-promo/post_*.py"))
        self.assertEqual(len(scripts), 6)
        for script in scripts:
            src = script.read_text(encoding="utf-8")
            self.assertIn("promo_broadcast.broadcast(", src, script)
            self.assertNotIn("send_delay=", src, script)

    def test_promo_cron_timeout_covers_worst_case_run(self):
        installer = (ROOT / "deploy/install-promo.sh").read_text(encoding="utf-8")
        self.assertNotIn("--timeout-seconds 600", installer)
        self.assertIn('PROMO_CRON_TIMEOUT_SECONDS="${PROMO_CRON_TIMEOUT_SECONDS:-3600}"',
                      installer)
        largest = 0
        for targets in CRON.glob("*-promo/targets.json"):
            data = json.loads(targets.read_text(encoding="utf-8"))
            jids = set(data.get("groups", [])) | set(data.get("weekly_groups", {}))
            largest = max(largest, len(jids))
        # Every gap at the 60 s max plus a generous 60 s per send must still fit.
        worst = (largest - 1) * pb.DEFAULT_SEND_DELAY_MAX + largest * 60
        self.assertLess(worst, 3600, "largest promo (%d groups) may exceed the timeout"
                        % largest)


class RunBudgetTests(SendSpacingTests):
    """Gaps shrink so a run always fits the cron job's timeout, and bad
    PROMO_SEND_DELAY_* values (inf/nan) fall back to the defaults."""

    def setUp(self):
        super().setUp()
        self.now = 0.0

    def _clock(self):
        return self.now

    def _sleep(self, seconds):
        super()._sleep(seconds)
        self.now += seconds

    def _fake_send(self, target, text, image):
        self.now += 10.0  # a slow media send
        return super()._fake_send(target, text, image)

    def test_default_budget_keeps_old_600s_jobs_alive(self):
        # Jobs registered before --run-budget existed carry a 600 s timeout and
        # pass no flag; the 540 s default must keep even worst-case draws inside.
        groups = ["%d@g.us" % i for i in range(26)]

        class MaxRng:
            def uniform(self, a, b):
                return b

        self.assertEqual(self._run(groups, rng=MaxRng(), clock=self._clock), 0)
        self.assertEqual(self.kinds(), ["send"] + ["sleep", "send"] * 25)
        self.assertLessEqual(self.now, pb.DEFAULT_RUN_BUDGET_SECONDS)
        self.assertGreater(len(set(self.sleeps())), 1, "gaps must not be constant")

    def test_installed_budget_keeps_full_range(self):
        groups = ["%d@g.us" % i for i in range(20)]
        rng = RecordingRng(seed=1)
        self.assertEqual(self._run(groups, argv=["--run-budget=3540"], rng=rng,
                                   clock=self._clock), 0)
        self.assertEqual(rng.calls, [(20.0, 60.0)] * 19)
        self.assertLess(self.now, 3540)

    def test_run_budget_parsing(self):
        self.assertEqual(pb.run_budget_seconds([]), pb.DEFAULT_RUN_BUDGET_SECONDS)
        self.assertEqual(pb.run_budget_seconds(["--run-budget", "3540"]), 3540.0)
        self.assertEqual(pb.run_budget_seconds(["--run-budget=900"]), 900.0)
        with redirect_stderr(io.StringIO()):
            for bad in ("inf", "nan", "-5", "0", "soon"):
                self.assertEqual(pb.run_budget_seconds(["--run-budget", bad]),
                                 pb.DEFAULT_RUN_BUDGET_SECONDS, bad)

    def test_fit_gap_bounds(self):
        self.assertEqual(pb.fit_gap_bounds(20, 60, 3000, 19), (20, 60))
        lo, hi = pb.fit_gap_bounds(20, 60, 500, 19)
        self.assertLess(hi, 60)
        self.assertLessEqual(lo, hi)
        self.assertLessEqual(19 * (hi + pb.SEND_RESERVE_SECONDS), 500 + 1e-9)
        self.assertEqual(pb.fit_gap_bounds(20, 60, 10, 5), (0.0, 0.0))

    def test_non_finite_env_values_fall_back_to_defaults(self):
        for lo_raw, hi_raw in (("inf", "inf"), ("nan", "nan"), ("20", "inf")):
            os.environ["PROMO_SEND_DELAY_MIN"] = lo_raw
            os.environ["PROMO_SEND_DELAY_MAX"] = hi_raw
            with redirect_stderr(io.StringIO()):
                lo, hi = pb.send_gap_bounds()
            self.assertEqual((lo, hi), (pb.DEFAULT_SEND_DELAY_MIN,
                                        pb.DEFAULT_SEND_DELAY_MAX), (lo_raw, hi_raw))

    def test_installer_passes_run_budget_matching_timeout(self):
        installer = (ROOT / "deploy/install-promo.sh").read_text(encoding="utf-8")
        self.assertIn('"--run-budget"', installer)
        self.assertIn("$((PROMO_CRON_TIMEOUT_SECONDS - 60))", installer)


if __name__ == "__main__":
    unittest.main()
