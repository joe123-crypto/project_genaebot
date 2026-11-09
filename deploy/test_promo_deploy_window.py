"""Tests for the deploy job's promo quiet-window gate."""

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
REAL_MANIFEST = json.loads((ROOT / "deploy/promo-crons.json").read_text(encoding="utf-8"))

# 20 groups: 900 s jitter + 20 x 12 s sends + 19 x 60 s gaps = 2280 s = 38 min.
TWENTY = 20


def promo(slug, cron, enabled=True):
    return {"slug": slug, "name": slug + "-promo", "cron": cron,
            "tz": "Africa/Harare", "enabled": enabled}


class GateTest(unittest.TestCase):
    def run_gate(self, manifest, now, *extra, targets=None):
        """Run against a scratch checkout; `targets` maps slug -> group count."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for slug, count in (targets or {}).items():
                folder = repo / "workspace/cron" / (slug + "-promo")
                folder.mkdir(parents=True)
                (folder / "targets.json").write_text(json.dumps(
                    {"groups": ["%d@g.us" % i for i in range(count)]}), encoding="utf-8")
            path = repo / "m.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            slept = []
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = gate.main(["--manifest", str(path), "--repo", str(repo), "--now", now, *extra],
                                 sleep=slept.append)
            return code, slept, out.getvalue() + err.getvalue()

    def run_real(self, now, *extra):
        out, err = io.StringIO(), io.StringIO()
        slept = []
        with redirect_stdout(out), redirect_stderr(err):
            code = gate.main(["--now", now, *extra], sleep=slept.append)
        return code, slept, out.getvalue() + err.getvalue()

    def test_the_2026_11_09_fruit_trees_release_would_have_waited(self):
        # #184's release restarted the gateway at 08:42 Harare, two minutes into
        # fruit-trees' 08:40 run. The gate runs a few minutes before the restart.
        code, slept, out = self.run_real("2026-11-09T06:35:00+00:00")
        self.assertEqual(code, 1)  # busy until well past the 45-minute bound
        self.assertEqual(slept, [])
        self.assertIn("fruit-trees-promo", out)
        self.assertIn("Next quiet window", out)

    def test_a_long_campaign_is_protected_past_its_first_hour(self):
        # fruit-trees' 46 groups can legitimately run ~69 min; 09:40 Harare is
        # one hour after it fired and must still be busy.
        code, _, out = self.run_real("2026-11-09T07:40:00+00:00", "--check-only")
        self.assertEqual(code, 1)
        self.assertIn("fruit-trees-promo", out)

    def test_quiet_gap_releases_immediately(self):
        manifest = {"promos": [promo("a", "0 9 * * *"), promo("b", "0 15 * * *")]}
        code, slept, out = self.run_gate(manifest, "2026-11-09T10:00:00+00:00",
                                         targets={"a": TWENTY, "b": TWENTY})  # 12:00 Harare
        self.assertEqual((code, slept), (0, []))
        self.assertIn("quiet", out)

    def test_short_wait_sleeps_until_the_run_budget_has_passed(self):
        manifest = {"promos": [promo("a", "0 9 * * *")]}
        # 09:30 Harare: a 38-minute budget ends at 09:38 -> 8 min wait.
        code, slept, _ = self.run_gate(manifest, "2026-11-09T07:30:00+00:00", targets={"a": TWENTY})
        self.assertEqual(code, 0)
        self.assertEqual(slept, [8 * 60])

    def test_upcoming_run_inside_the_lead_is_busy(self):
        manifest = {"promos": [promo("a", "0 9 * * *")]}
        # 08:50 Harare, run at 09:00: busy until 09:38, beyond the wait bound.
        code, slept, out = self.run_gate(manifest, "2026-11-09T06:50:00+00:00", targets={"a": TWENTY})
        self.assertEqual((code, slept), (1, []))
        self.assertIn("09:38 CAT", out)

    def test_campaign_without_targets_is_given_its_whole_timeout(self):
        manifest = {"timeoutSeconds": 7200, "promos": [promo("a", "0 9 * * *")]}
        # 10:50 Harare: past any group-sized budget, still inside the 2 h timeout.
        code, _, out = self.run_gate(manifest, "2026-11-09T08:50:00+00:00", "--check-only")
        self.assertEqual(code, 1)
        self.assertIn("11:00 CAT", out)

    def test_budget_is_capped_at_the_job_timeout(self):
        budget = gate.run_budget_minutes({"slug": "x"}, 600, repo=Path("/nonexistent"))
        self.assertEqual(budget, 10)

    def test_disabled_campaigns_are_ignored(self):
        manifest = {"promos": [promo("cv", "39 11 * * *", enabled=False)]}
        code, _, _ = self.run_gate(manifest, "2026-11-09T09:40:00+00:00")
        self.assertEqual(code, 0)

    def test_runs_across_midnight_are_seen(self):
        manifest = {"promos": [promo("late", "50 23 * * *")]}
        code, _, _ = self.run_gate(manifest, "2026-11-09T22:20:00+00:00", "--max-wait-minutes", "0",
                                   targets={"late": TWENTY})  # 00:20 Harare
        self.assertEqual(code, 1)

    def test_check_only_never_sleeps(self):
        manifest = {"promos": [promo("a", "0 9 * * *")]}
        code, slept, _ = self.run_gate(manifest, "2026-11-09T07:30:00+00:00", "--check-only",
                                       targets={"a": TWENTY})
        self.assertEqual((code, slept), (1, []))

    def test_non_daily_schedule_is_rejected(self):
        manifest = {"promos": [promo("weekly", "0 9 * * 1")]}
        code, _, out = self.run_gate(manifest, "2026-11-09T10:00:00+00:00")
        self.assertEqual(code, 2)
        self.assertIn("daily", out)

    def test_every_manifest_campaign_is_sized_from_its_targets(self):
        for promo_entry in REAL_MANIFEST["promos"]:
            self.assertIsNotNone(gate.target_count(promo_entry), promo_entry["slug"])
        self.assertTrue(gate.daily_starts(REAL_MANIFEST))

    def test_engine_timing_mirrors_match_promo_broadcast(self):
        self.assertEqual(gate.START_JITTER_SECONDS, promo_broadcast.DEFAULT_START_JITTER_SECONDS)
        self.assertEqual(gate.SEND_GAP_MAX_SECONDS, promo_broadcast.DEFAULT_SEND_DELAY_MAX)
        self.assertEqual(gate.SEND_ALLOWANCE_SECONDS, promo_broadcast.SEND_ALLOWANCE_SECONDS)

    def test_deploy_job_gates_before_the_release(self):
        deploy = WORKFLOW[WORKFLOW.index("\n  deploy:\n"):]
        gate_at = deploy.index("python3 deploy/promo-deploy-window.py")
        self.assertLess(gate_at, deploy.index("python3 deploy/railway-release.py"))
        self.assertIn("inputs.ignore_promo_window", deploy)


if __name__ == "__main__":
    unittest.main()
