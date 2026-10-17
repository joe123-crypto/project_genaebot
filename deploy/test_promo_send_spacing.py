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
        kwargs.setdefault("cron_timeout", None)  # no gateway lookup in tests
        # Off unless a test asks for it, so the gap assertions below see only
        # the between-send gaps. StartJitterTests covers it explicitly.
        kwargs.setdefault("start_jitter", 0)
        kwargs.setdefault("caps", None)  # per-group caps have their own tests
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
        self.assertEqual(self._run(groups, rng=rng), 0)
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

    def test_non_finite_env_falls_back_to_defaults(self):
        """inf would sleep until the job timeout killed the run; nan used to slip
        past the negative check and collapse the gap to zero, silently removing
        the spacing this module exists for."""
        for value in ("inf", "-inf", "nan", "NaN"):
            os.environ["PROMO_SEND_DELAY_MIN"] = value
            os.environ["PROMO_SEND_DELAY_MAX"] = value
            with redirect_stderr(io.StringIO()) as err:
                self.assertEqual(pb.send_gap_bounds(), (20.0, 60.0), value)
            self.assertIn("WARN", err.getvalue(), value)

    def test_non_finite_arguments_fall_back_to_defaults(self):
        inf = float("inf")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(pb.send_gap_bounds(send_delay_min=inf), (20.0, 60.0))
            self.assertEqual(pb.send_gap_bounds(send_delay=float("nan")),
                             (20.0, 20.0))

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
        self.assertEqual(len(scripts), 7)
        for script in scripts:
            src = script.read_text(encoding="utf-8")
            self.assertIn("promo_broadcast.broadcast(", src, script)
            self.assertNotIn("send_delay=", src, script)

    def test_promo_cron_timeout_covers_worst_case_run(self):
        installer = (ROOT / "deploy/install-promo.sh").read_text(encoding="utf-8")
        self.assertNotIn("--timeout-seconds 600", installer)
        self.assertIn('PROMO_CRON_TIMEOUT_SECONDS="${PROMO_CRON_TIMEOUT_SECONDS:-5400}"',
                      installer)
        # Re-running an installer must prove the timeout reached the volume's
        # cron state, not just that the job exists.
        self.assertIn(".payload.timeoutSeconds == $timeout", installer)
        largest = 0
        for targets in CRON.glob("*-promo/targets.json"):
            data = json.loads(targets.read_text(encoding="utf-8"))
            jids = set(data.get("groups", [])) | set(data.get("weekly_groups", {}))
            largest = max(largest, len(jids))
        # Every gap at the 60 s max plus a generous 60 s per send must still fit.
        worst = (largest - 1) * pb.DEFAULT_SEND_DELAY_MAX + largest * 60
        self.assertLess(worst, 5400, "largest promo (%d groups) may exceed the timeout"
                        % largest)


class RunTimeoutFitTests(unittest.TestCase):
    """A promo cron's job timeout lives in the gateway's cron state on the
    volume, so merging a longer one into deploy/install-promo.sh does not change
    the live jobs until the installers are re-run -- and no deploy step runs
    them. Under the old 600 s timeout a 20-group promo at 20-60 s gaps takes
    ~15 min and would be killed with a third of its groups unreached, every day.
    The engine therefore reads its own job's timeout and tightens the gaps to
    fit instead."""

    def test_ample_timeout_leaves_bounds_alone(self):
        self.assertEqual(pb.fit_gap_bounds(20.0, 60.0, 20, 3600), (20.0, 60.0))

    def test_unknown_or_single_send_is_a_no_op(self):
        self.assertEqual(pb.fit_gap_bounds(20.0, 60.0, 20, None), (20.0, 60.0))
        self.assertEqual(pb.fit_gap_bounds(20.0, 60.0, 1, 600), (20.0, 60.0))
        self.assertEqual(pb.fit_gap_bounds(0.0, 0.0, 20, 600), (0.0, 0.0))

    def test_stale_timeout_tightens_gaps_enough_to_finish(self):
        with redirect_stderr(io.StringIO()) as err:
            lo, hi = pb.fit_gap_bounds(20.0, 60.0, 20, 600)
        self.assertLess(hi, 60.0)
        self.assertLess(lo, hi, "gaps must stay a random range, not a fixed cadence")
        self.assertGreater(lo, 0.0)
        worst = (20 - 1) * hi + 20 * pb.SEND_ALLOWANCE_SECONDS
        self.assertLessEqual(worst, 600)
        self.assertIn("install-", err.getvalue())  # names the remedy

    def test_every_promo_size_fits_the_legacy_timeout_when_tightened(self):
        for count in range(2, 26):
            with redirect_stderr(io.StringIO()):
                lo, hi = pb.fit_gap_bounds(20.0, 60.0, count, 600)
            worst = (count - 1) * hi + count * pb.SEND_ALLOWANCE_SECONDS
            self.assertLessEqual(worst, 600, "%d groups" % count)
            self.assertLessEqual(lo, hi, "%d groups" % count)

    def test_a_timeout_that_cannot_fit_even_the_floor_says_so(self):
        """The squeeze promises a complete run; below min_timeout_seconds it
        cannot keep that promise, so it must not claim to (Codex P2 on #108)."""
        count, timeout = 20, 300
        self.assertGreater(pb.min_timeout_seconds(count), timeout)
        with redirect_stderr(io.StringIO()) as err:
            lo, hi = pb.fit_gap_bounds(20.0, 60.0, count, timeout)
        self.assertEqual(hi, pb.MIN_SEND_DELAY)
        self.assertLessEqual(lo, hi)
        warning = err.getvalue()
        self.assertIn("may still be killed", warning)
        self.assertNotIn("every group still gets", warning)
        self.assertIn("%.0f s" % pb.min_timeout_seconds(count), warning)

    def test_min_timeout_seconds_is_the_point_the_run_becomes_feasible(self):
        for count in range(2, 26):
            needed = pb.min_timeout_seconds(count)
            with redirect_stderr(io.StringIO()) as err:
                lo, hi = pb.fit_gap_bounds(20.0, 60.0, count, needed)
            self.assertGreaterEqual(hi, pb.MIN_SEND_DELAY, count)
            self.assertNotIn("may still be killed", err.getvalue(), count)
            worst = (count - 1) * hi + count * pb.SEND_ALLOWANCE_SECONDS
            self.assertLessEqual(round(worst, 6), needed, "%d groups" % count)

    def test_squeezed_lower_bound_also_respects_the_floor(self):
        for timeout in (373, 420, 600, 900):
            with redirect_stderr(io.StringIO()):
                lo, hi = pb.fit_gap_bounds(20.0, 60.0, 20, timeout)
            self.assertGreaterEqual(lo, pb.MIN_SEND_DELAY, timeout)
            self.assertLessEqual(lo, hi, timeout)

    def test_a_range_configured_below_the_floor_keeps_its_shape(self):
        with redirect_stderr(io.StringIO()):
            lo, hi = pb.fit_gap_bounds(1.0, 3.0, 20, 600)
        self.assertLess(lo, hi, "a sub-floor range must not be pushed to fixed")

    def test_the_warning_tracks_whether_the_plan_actually_fits(self):
        """One source of truth: the message is chosen from the planned run's
        worst case, so no branch can promise a complete run it cannot deliver."""
        for lo, hi, count, timeout, fits in (
                (20.0, 60.0, 20, 600, True),    # live jobs today
                (20.0, 60.0, 20, 300, False),
                (1.0, 3.0, 20, 120, False),     # range configured below the floor
        ):
            with redirect_stderr(io.StringIO()) as err:
                new_lo, new_hi = pb.fit_gap_bounds(lo, hi, count, timeout)
            worst = (count - 1) * new_hi + count * pb.SEND_ALLOWANCE_SECONDS
            budget = timeout * pb.RUN_BUDGET_FRACTION
            self.assertEqual(worst <= budget + 1e-6, fits, (lo, hi, count, timeout))
            self.assertEqual("every group still gets" in err.getvalue(), fits,
                             err.getvalue())

    def test_gap_never_shrinks_below_the_floor(self):
        with redirect_stderr(io.StringIO()):
            lo, hi = pb.fit_gap_bounds(20.0, 60.0, 50, 60)
        self.assertEqual(hi, pb.MIN_SEND_DELAY)
        self.assertLessEqual(lo, hi)

    def test_broadcast_tightens_gaps_but_still_sends_every_group(self):
        tests = SendSpacingTests("test_single_send_never_sleeps")
        tests.setUp()
        try:
            rng = RecordingRng(seed=7)
            groups = ["%d@g.us" % i for i in range(20)]
            with redirect_stderr(io.StringIO()):
                rc = tests._run(groups, rng=rng, cron_timeout=600)
            self.assertEqual(rc, 0)
            self.assertEqual(len([e for e in tests.events if e[0] == "send"]), 20)
            (lo, hi), = set(rng.calls)
            self.assertLess(hi, 60.0)
            self.assertLess(lo, hi)
            run = sum(v for kind, v in tests.events if kind == "sleep") + 20 * 7
            self.assertLess(run, 600, "tightened run must fit the live timeout")
        finally:
            tests.tearDown()


class MaxRng:
    """Always draws the top of the range, so jitter is deterministic."""

    def __init__(self):
        self.calls = []

    def uniform(self, a, b):
        self.calls.append((a, b))
        return b


class StartJitterTests(unittest.TestCase):
    """Each promo cron fires at one fixed minute, so without a random head start
    the account's day-to-day pattern stays perfectly periodic even though the
    gaps inside a run are randomised. The engine waits a random 0-900 s before
    its FIRST send -- charged against the job's timeout budget, never taken on
    --dry-run/--refs, and never taken when there is nothing to send."""

    def setUp(self):
        self.tests = SendSpacingTests("test_single_send_never_sleeps")
        self.tests.setUp()
        self.addCleanup(self.tests.tearDown)
        # SendSpacingTests only restores the gap env vars, so this one has to
        # clean up after itself or it leaks into the next test.
        saved = os.environ.pop("PROMO_START_JITTER_SECONDS", None)
        self.addCleanup(lambda: os.environ.__setitem__(
            "PROMO_START_JITTER_SECONDS", saved) if saved is not None
            else os.environ.pop("PROMO_START_JITTER_SECONDS", None))

    def run_promo(self, groups=("1@g.us", "2@g.us"), **kwargs):
        with redirect_stderr(io.StringIO()):
            rc = self.tests._run(list(groups), **kwargs)
        self.assertEqual(rc, 0)
        return self.tests.events

    def test_default_bound_is_fifteen_minutes(self):
        self.assertEqual(pb.start_jitter_bound(), 900.0)

    def test_a_wait_is_taken_before_the_first_send(self):
        rng = MaxRng()
        events = self.run_promo(rng=rng, start_jitter=None, send_delay=0)
        self.assertEqual(events[0], ("sleep", 900.0))
        self.assertEqual(events[1][0], "send")
        self.assertEqual(rng.calls[0], (0, 900.0))

    def test_zero_disables_it_without_touching_the_rng(self):
        rng = MaxRng()
        events = self.run_promo(rng=rng, start_jitter=0, send_delay=0)
        self.assertEqual(events[0][0], "send", "no wait before the first send")
        # The only draws left are the between-send gaps (pinned to 0 here).
        self.assertEqual(rng.calls, [(0.0, 0.0)],
                         "a disabled jitter must not draw its own range")

    def test_env_var_sets_the_bound(self):
        os.environ["PROMO_START_JITTER_SECONDS"] = "120"
        rng = MaxRng()
        self.run_promo(rng=rng, start_jitter=None, send_delay=0)
        self.assertEqual(rng.calls[0], (0, 120.0))

    def test_bad_env_falls_back_to_the_default(self):
        os.environ["PROMO_START_JITTER_SECONDS"] = "soonish"
        with redirect_stderr(io.StringIO()):
            self.assertEqual(pb.start_jitter_bound(), 900.0)

    def test_not_taken_on_dry_run(self):
        events = self.run_promo(argv=["--dry-run"], rng=MaxRng(), start_jitter=None)
        self.assertEqual([e for e in events if e[0] == "sleep"], [])

    def test_not_taken_on_refs(self):
        events = self.run_promo(argv=["--refs"], rng=MaxRng(), start_jitter=None,
                                tracking={"prefix": "BH", "number": "+263"})
        self.assertEqual([e for e in events if e[0] == "sleep"], [])

    def test_not_taken_when_every_group_already_got_todays_ad(self):
        self.run_promo(rng=MaxRng(), start_jitter=0, send_delay=0)
        self.tests.events.clear()
        events = self.run_promo(rng=MaxRng(), start_jitter=None, send_delay=0)
        self.assertEqual(events, [], "nothing pending: no wait, no sends")

    def test_todays_promo_sizes_pay_nothing_for_the_head_start(self):
        """At today's biggest promo (20 groups) a full 15-minute head start
        still leaves room for the full 20-60 s gaps, so enabling the jitter
        changes no gap."""
        groups = ["%d@g.us" % i for i in range(20)]
        rng = MaxRng()
        self.run_promo(groups, rng=rng, start_jitter=None, cron_timeout=3600)
        self.assertEqual(rng.calls[0], (0, 900.0))
        self.assertEqual(set(rng.calls[1:]), {(20.0, 60.0)})

    def test_it_is_charged_against_the_timeout_budget(self):
        """A 15-minute head start must not eat the margin fit_gap_bounds exists
        to protect: once a promo is big enough for the budget to bind, the gaps
        are planned with what the jitter leaves, not the whole timeout."""
        groups = ["%d@g.us" % i for i in range(45)]
        with_jitter = MaxRng()
        self.run_promo(groups, rng=with_jitter, start_jitter=None,
                       cron_timeout=3600)
        self.tests.events.clear()

        self.setUp()  # fresh state file, so all 45 are pending again
        without = MaxRng()
        self.run_promo(groups, rng=without, start_jitter=0, cron_timeout=3600)

        self.assertEqual(with_jitter.calls[0], (0, 900.0))
        (lo, hi), = set(with_jitter.calls[1:])
        self.assertEqual(set(without.calls), {(20.0, 60.0)},
                         "without the head start these gaps fit untouched")
        self.assertLess(hi, 60.0, "gaps must tighten to pay for the head start")
        worst = 900.0 + (len(groups) - 1) * hi + len(groups) * pb.SEND_ALLOWANCE_SECONDS
        self.assertLessEqual(worst, 3600, "the whole run must still fit")

    def test_a_stale_short_timeout_clamps_the_wait_instead_of_losing_the_run(self):
        rng = MaxRng()
        events = self.run_promo(["1@g.us", "2@g.us", "3@g.us"], rng=rng,
                                start_jitter=None, cron_timeout=600)
        waited = events[0][1]
        self.assertEqual(events[0][0], "sleep")
        self.assertEqual(waited, 600 * pb.START_JITTER_BUDGET_FRACTION)
        self.assertEqual(rng.calls[0], (0, 600 * pb.START_JITTER_BUDGET_FRACTION),
                         "the range shrinks; the draw is not clamped")
        self.assertEqual(len([e for e in events if e[0] == "send"]), 3,
                         "every group must still get today's ad")

    def test_a_stale_short_timeout_still_gives_a_random_wait(self):
        """Clamping the draw made the wait a constant: on 2026-10-16, under the
        live 600 s timeout, all five measured promo runs sent their first ad
        ~2m38s past the hour. Shrinking the range keeps it random."""
        rng = random.Random(1)
        waits = set()
        for _ in range(12):
            self.setUp()  # fresh state file, so the groups are pending again
            events = self.run_promo(["1@g.us", "2@g.us"], rng=rng,
                                    start_jitter=None, send_delay=0,
                                    cron_timeout=600)
            self.assertEqual(events[0][0], "sleep")
            self.assertLessEqual(events[0][1], 600 * pb.START_JITTER_BUDGET_FRACTION)
            waits.add(round(events[0][1], 3))
        self.assertGreater(len(waits), 10, "waits must vary run to run")

    def test_the_whole_run_still_fits_for_every_promo_size(self):
        for count in (1, 5, 10, 20, 25):
            jitter = min(900.0, 3600 * pb.START_JITTER_BUDGET_FRACTION)
            with redirect_stderr(io.StringIO()):
                lo, hi = pb.fit_gap_bounds(20.0, 60.0, count, 3600 - jitter)
            worst = jitter + (count - 1) * hi + count * pb.SEND_ALLOWANCE_SECONDS
            self.assertLessEqual(worst, 3600, count)


class CronTimeoutLookupTests(unittest.TestCase):
    JOBS = {"jobs": [
        {"name": "other-promo", "payload": {
            "argv": ["python3", "/w/cron/other-promo/post_ad.py"],
            "timeoutSeconds": 123}},
        {"name": "cv-promo", "payload": {
            "argv": ["python3", "/home/joseph/.openclaw/workspace/cron/cv-promo/post_cv_ad.py"],
            "timeoutSeconds": 600}},
    ]}

    def setUp(self):
        self._saved_run = pb.subprocess.run
        self._saved_env = os.environ.pop("PROMO_RUN_TIMEOUT_SECONDS", None)

    def tearDown(self):
        pb.subprocess.run = self._saved_run
        if self._saved_env is None:
            os.environ.pop("PROMO_RUN_TIMEOUT_SECONDS", None)
        else:
            os.environ["PROMO_RUN_TIMEOUT_SECONDS"] = self._saved_env

    def _stub(self, stdout="", returncode=0, raises=None):
        def run(cmd, **kwargs):
            if raises:
                raise raises
            return type("P", (), {"returncode": returncode, "stdout": stdout,
                                  "stderr": ""})()
        pb.subprocess.run = run

    def test_reads_the_timeout_of_this_script_s_own_job(self):
        self._stub(json.dumps(self.JOBS))
        with redirect_stderr(io.StringIO()):
            self.assertEqual(
                pb.cron_timeout_seconds("/any/where/cv-promo/post_cv_ad.py"), 600.0)

    def test_no_matching_job_means_no_limit(self):
        self._stub(json.dumps(self.JOBS))
        with redirect_stderr(io.StringIO()):
            self.assertIsNone(
                pb.cron_timeout_seconds("/any/where/manual-run/post_ad.py"))

    def test_lookup_failures_are_not_fatal(self):
        for stub in ({"stdout": "not json"}, {"returncode": 1},
                     {"raises": OSError("boom")}, {"stdout": "{}"}):
            self._stub(**stub)
            with redirect_stderr(io.StringIO()):
                self.assertIsNone(
                    pb.cron_timeout_seconds("/w/cv-promo/post_cv_ad.py"), stub)

    def test_job_without_a_timeout_is_unbounded(self):
        self._stub(json.dumps({"jobs": [{"name": "cv-promo", "payload": {
            "argv": ["python3", "/w/cron/cv-promo/post_cv_ad.py"]}}]}))
        with redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(pb.cron_timeout_seconds("/w/cv-promo/post_cv_ad.py"))
        self.assertNotIn("timeoutSeconds", err.getvalue())  # absent, not invalid

    def test_a_bare_script_name_matches_nothing(self):
        """Four promos share the file name post_ad.py, so a path with no promo
        directory must not match one of them by accident."""
        self._stub(json.dumps(self.JOBS))
        self.assertIsNone(pb.cron_timeout_seconds("/post_cv_ad.py"))

    def test_env_override_wins_and_zero_means_unlimited(self):
        self._stub(json.dumps(self.JOBS))
        os.environ["PROMO_RUN_TIMEOUT_SECONDS"] = "1200"
        self.assertEqual(pb.cron_timeout_seconds("/w/cv-promo/post_cv_ad.py"), 1200.0)
        os.environ["PROMO_RUN_TIMEOUT_SECONDS"] = "0"
        self.assertIsNone(pb.cron_timeout_seconds("/w/cv-promo/post_cv_ad.py"))
        os.environ["PROMO_RUN_TIMEOUT_SECONDS"] = "later"
        with redirect_stderr(io.StringIO()):  # rejected -> falls back to lookup
            self.assertEqual(pb.cron_timeout_seconds("/w/cv-promo/post_cv_ad.py"),
                             600.0)


if __name__ == "__main__":
    unittest.main()
