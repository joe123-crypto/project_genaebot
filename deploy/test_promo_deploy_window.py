"""Tests for the deploy job's promo quiet-window gate."""

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("promo_deploy_window", ROOT / "deploy/promo-deploy-window.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
WORKFLOW = (ROOT / ".github/workflows/deploy-main.yml").read_text(encoding="utf-8")


def promo(name, cron, enabled=True):
    return {"name": name, "cron": cron, "tz": "Africa/Harare", "enabled": enabled}


class GateTest(unittest.TestCase):
    def run_gate(self, manifest, now, *extra):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            slept = []
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = gate.main(["--manifest", str(path), "--now", now, *extra], sleep=slept.append)
            return code, slept, out.getvalue() + err.getvalue()

    def test_the_2026_11_09_fruit_trees_release_would_have_waited(self):
        # #184's release restarted the gateway at 08:42 Harare, two minutes into
        # fruit-trees' 08:40 run. The gate runs a few minutes before the restart.
        manifest = json.loads((ROOT / "deploy/promo-crons.json").read_text(encoding="utf-8"))
        code, slept, out = self.run_gate(manifest, "2026-11-09T06:35:00+00:00")
        self.assertEqual(code, 1)  # busy until well past the 45-minute bound
        self.assertEqual(slept, [])
        self.assertIn("fruit-trees-promo", out)
        self.assertIn("Next quiet window", out)

    def test_quiet_gap_releases_immediately(self):
        manifest = {"promos": [promo("a-promo", "0 9 * * *"), promo("b-promo", "0 15 * * *")]}
        code, slept, out = self.run_gate(manifest, "2026-11-09T10:00:00+00:00")  # 12:00 Harare
        self.assertEqual((code, slept), (0, []))
        self.assertIn("quiet", out)

    def test_short_wait_sleeps_until_the_run_has_had_its_hour(self):
        manifest = {"promos": [promo("a-promo", "0 9 * * *")]}
        # 09:30 Harare: started 30 min ago, quiet at 10:00 -> 30 min wait.
        code, slept, _ = self.run_gate(manifest, "2026-11-09T07:30:00+00:00")
        self.assertEqual(code, 0)
        self.assertEqual(slept, [30 * 60])

    def test_upcoming_run_inside_the_lead_is_busy(self):
        manifest = {"promos": [promo("a-promo", "0 9 * * *")]}
        # 08:50 Harare, run at 09:00: busy until 10:00, beyond the wait bound.
        code, slept, out = self.run_gate(manifest, "2026-11-09T06:50:00+00:00")
        self.assertEqual((code, slept), (1, []))
        self.assertIn("10:00 CAT", out)

    def test_disabled_campaigns_are_ignored(self):
        manifest = {"promos": [promo("cv-promo", "39 11 * * *", enabled=False)]}
        code, _, _ = self.run_gate(manifest, "2026-11-09T09:40:00+00:00")
        self.assertEqual(code, 0)

    def test_runs_across_midnight_are_seen(self):
        manifest = {"promos": [promo("late-promo", "50 23 * * *")]}
        code, _, _ = self.run_gate(manifest, "2026-11-09T22:20:00+00:00", "--max-wait-minutes", "0")  # 00:20 Harare
        self.assertEqual(code, 1)

    def test_check_only_never_sleeps(self):
        manifest = {"promos": [promo("a-promo", "0 9 * * *")]}
        code, slept, _ = self.run_gate(manifest, "2026-11-09T07:30:00+00:00", "--check-only")
        self.assertEqual((code, slept), (1, []))

    def test_non_daily_schedule_is_rejected(self):
        manifest = {"promos": [promo("weekly-promo", "0 9 * * 1")]}
        code, _, out = self.run_gate(manifest, "2026-11-09T10:00:00+00:00")
        self.assertEqual(code, 2)
        self.assertIn("daily", out)

    def test_every_manifest_schedule_is_understood(self):
        manifest = json.loads((ROOT / "deploy/promo-crons.json").read_text(encoding="utf-8"))
        self.assertTrue(gate.daily_starts(manifest))

    def test_deploy_job_gates_before_the_release(self):
        deploy = WORKFLOW[WORKFLOW.index("\n  deploy:\n"):]
        gate_at = deploy.index("python3 deploy/promo-deploy-window.py")
        self.assertLess(gate_at, deploy.index("python3 deploy/railway-release.py"))
        self.assertIn("inputs.ignore_promo_window", deploy)


if __name__ == "__main__":
    unittest.main()
