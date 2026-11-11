"""Tests for the deploy job's promo quiet-window gate.

The gate's job has not changed -- hold a release until no campaign may be
sending -- but where it learns the campaigns has: the schedules and group counts
were committed files and are now read from the gateway, so the fixtures here are
``console-read campaigns`` replies rather than a manifest and a tree of
targets.json. The timing cases are the same ones, including the 2026-11-09
incident this gate was written for.
"""

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import promo_broadcast

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("promo_deploy_window", ROOT / "deploy/promo-deploy-window.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
WORKFLOW = (ROOT / ".github/workflows/deploy-main.yml").read_text(encoding="utf-8")

# 20 groups: 900 s jitter + 20 x 12 s sends + 19 x 60 s gaps = 2280 s = 38 min.
TWENTY = 20


def row(slug, hour, minute, *, count=TWENTY, enabled=True, timeout=7200,
        job=None, tz="Africa/Harare"):
    """One campaign, shaped as console-read reports it."""
    return {"slug": slug, "name": slug, "jobName": job or (slug + "-promo"),
            "enabled": enabled, "hour": hour, "minute": minute, "timezone": tz,
            "targetCount": count, "timeoutSeconds": timeout, "source": "console"}


class GateTest(unittest.TestCase):
    def run_gate(self, rows, now, *extra):
        slept = []
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = gate.main(["--now", now, *extra], sleep=slept.append,
                             read=lambda: gate.gateway_campaigns.parse(
                                 json.dumps({"campaigns": rows})))
        return code, slept, out.getvalue() + err.getvalue()

    # ---------------------------------------------------------- the incident --
    def test_the_2026_11_09_fruit_trees_release_would_have_waited(self):
        # #184's release restarted the gateway at 08:42 Harare, two minutes into
        # fruit-trees' 08:40 run, and fruit-trees posted to no group that day.
        rows = [row("fruit-trees", 8, 40, count=46)]
        code, slept, out = self.run_gate(rows, "2026-11-09T06:35:00+00:00")
        self.assertEqual(code, 1)  # busy well past the 45-minute bound
        self.assertEqual(slept, [])
        self.assertIn("fruit-trees-promo", out)
        self.assertIn("Next quiet window", out)

    def test_a_long_campaign_is_protected_past_its_first_hour(self):
        # 46 groups can legitimately run ~69 min, so one hour after it fired is
        # still busy.
        code, _, out = self.run_gate([row("fruit-trees", 8, 40, count=46)],
                                     "2026-11-09T07:40:00+00:00", "--check-only")
        self.assertEqual(code, 1)
        self.assertIn("fruit-trees-promo", out)

    # ------------------------------------------------------------- the timing --
    def test_quiet_gap_releases_immediately(self):
        code, slept, out = self.run_gate([row("a", 9, 0), row("b", 15, 0)],
                                         "2026-11-09T10:00:00+00:00")  # 12:00 Harare
        self.assertEqual((code, slept), (0, []))
        self.assertIn("quiet", out)

    def test_short_wait_sleeps_until_the_run_budget_has_passed(self):
        # 09:30 Harare: a 38-minute budget ends at 09:38 -> 8 min wait.
        code, slept, _ = self.run_gate([row("a", 9, 0)], "2026-11-09T07:30:00+00:00")
        self.assertEqual(code, 0)
        self.assertEqual(slept, [8 * 60])

    def test_upcoming_run_inside_the_lead_is_busy(self):
        # 08:50 Harare, run at 09:00: busy until 09:38, beyond the wait bound.
        code, slept, out = self.run_gate([row("a", 9, 0)], "2026-11-09T06:50:00+00:00")
        self.assertEqual((code, slept), (1, []))
        self.assertIn("09:38 CAT", out)

    def test_budget_is_capped_at_the_job_timeout(self):
        # 400 groups would run for hours; the job is killed at its timeout.
        self.assertEqual(gate.run_budget_minutes(row("x", 9, 0, count=400, timeout=600)), 10)

    def test_disabled_campaigns_are_ignored(self):
        code, _, _ = self.run_gate([row("cv", 11, 39, enabled=False)],
                                   "2026-11-09T09:40:00+00:00")
        self.assertEqual(code, 0)

    def test_runs_across_midnight_are_seen(self):
        code, _, _ = self.run_gate([row("late", 23, 50)], "2026-11-09T22:20:00+00:00",
                                   "--max-wait-minutes", "0")  # 00:20 Harare
        self.assertEqual(code, 1)

    def test_check_only_never_sleeps(self):
        code, slept, _ = self.run_gate([row("a", 9, 0)], "2026-11-09T07:30:00+00:00",
                                       "--check-only")
        self.assertEqual((code, slept), (1, []))

    # --------------------------------------------------- what reading the live --
    #                                                      list changed
    def test_a_console_created_campaign_is_now_held_for(self):
        # The blind spot this gate shipped with: a campaign created in the
        # console was in no committed file, so the gate never held a release out
        # of its sending window.
        code, _, out = self.run_gate([row("web-design", 8, 30, count=42,
                                          job="console-web-design-promo")],
                                     "2026-11-09T06:35:00+00:00", "--check-only")
        self.assertEqual(code, 1)
        self.assertIn("console-web-design-promo", out)

    def test_a_live_schedule_override_is_what_is_honoured(self):
        # The gate sees the time actually in force, not a shipped default: a
        # campaign moved to 14:05 must not be held for at its old 08:40 slot.
        rows = [row("fruit-trees", 14, 5, count=46)]
        code, _, _ = self.run_gate(rows, "2026-11-09T06:35:00+00:00", "--check-only")
        self.assertEqual(code, 0)

    def test_an_unreachable_gateway_refuses_rather_than_guesses(self):
        def explode():
            raise gate.gateway_campaigns.Unavailable("railway ssh: no such service")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = gate.main(["--now", "2026-11-09T03:00:00+00:00"], read=explode)
        text = out.getvalue() + err.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("cannot tell", text)
        self.assertIn("ignore_promo_window", text)

    def test_a_campaign_missing_its_schedule_is_refused(self):
        incomplete = row("a", 9, 0)
        del incomplete["targetCount"]
        code, _, out = self.run_gate([incomplete], "2026-11-09T03:00:00+00:00")
        self.assertEqual(code, 1)
        self.assertIn("targetCount", out)

    def test_an_unusable_timezone_is_named(self):
        code, _, out = self.run_gate([row("a", 9, 0, tz="Mars/Olympus")],
                                     "2026-11-09T03:00:00+00:00")
        self.assertEqual(code, 2)
        self.assertIn("Mars/Olympus", out)

    def test_a_saved_reply_can_be_replayed_from_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "campaigns.json")
            path.write_text(json.dumps({"campaigns": [row("a", 9, 0)]}), encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = gate.main(["--campaigns", str(path), "--check-only",
                                  "--now", "2026-11-09T06:50:00+00:00"])
            self.assertEqual(code, 1)
            self.assertIn("09:38 CAT", out.getvalue() + err.getvalue())

    # ------------------------------------------------------------- the wiring --
    def test_engine_timing_mirrors_match_promo_broadcast(self):
        mirror = gate.gateway_campaigns
        self.assertEqual(mirror.START_JITTER_SECONDS, promo_broadcast.DEFAULT_START_JITTER_SECONDS)
        self.assertEqual(mirror.SEND_GAP_MAX_SECONDS, promo_broadcast.DEFAULT_SEND_DELAY_MAX)
        self.assertEqual(mirror.SEND_ALLOWANCE_SECONDS, promo_broadcast.SEND_ALLOWANCE_SECONDS)

    def test_deploy_job_gates_before_the_release(self):
        deploy = WORKFLOW[WORKFLOW.index("\n  deploy:\n"):]
        gate_at = deploy.index("python3 deploy/promo-deploy-window.py")
        self.assertLess(gate_at, deploy.index("python3 deploy/railway-release.py"))
        self.assertIn("inputs.ignore_promo_window", deploy)

    def test_the_gate_step_is_given_the_token_it_now_needs(self):
        # Reading the live campaigns means `railway ssh`, which needs the token
        # the release step already has; without it the gate refuses every release.
        deploy = WORKFLOW[WORKFLOW.index("\n  deploy:\n"):]
        step = deploy[deploy.index("- name: Wait for a quiet promo window"):]
        step = step[:step.index("      - name: Release or restore")]
        self.assertIn("RAILWAY_TOKEN: ${{ secrets.RAILWAY_TOKEN }}", step)


if __name__ == "__main__":
    unittest.main()
