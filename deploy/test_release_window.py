#!/usr/bin/env python3
"""The answer a bot gives when asked whether it may be restarted now.

The arithmetic is the release gate's own and is tested with it
(test_promo_deploy_window.py); what is pinned here is the shape the operator
console reads, and the two edges that matter to it: a bot with no campaigns is
quiet, and a bot inside a run says when it will next be quiet.
"""

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_window  # noqa: E402

UTC = dt.timezone.utc


def row(slug, hour, minute, *, count=20, enabled=True, tz="Africa/Harare"):
    return {"slug": slug, "name": slug, "jobName": slug + "-promo", "enabled": enabled,
            "hour": hour, "minute": minute, "timezone": tz, "targetCount": count,
            "timeoutSeconds": 7200, "source": "console"}


class WindowTest(unittest.TestCase):
    def test_a_bot_with_no_campaigns_is_quiet_now(self):
        now = dt.datetime(2026, 11, 26, 10, 0, tzinfo=UTC)
        self.assertEqual(release_window.window([], now), {
            "quiet": True, "at": "2026-11-26T10:00:00Z", "nextQuietAt": "2026-11-26T10:00:00Z",
            "leadMinutes": 20, "busy": []})

    def test_a_run_in_progress_holds_the_restart_and_says_until_when(self):
        # 12:00 Harare is 10:00 UTC. Twenty groups may send for 38 minutes.
        rows = [row("borehole", 12, 0)]
        now = dt.datetime(2026, 11, 26, 10, 10, tzinfo=UTC)
        answer = release_window.window(rows, now)
        self.assertFalse(answer["quiet"])
        self.assertEqual(answer["busy"], [{"name": "borehole-promo", "start": "2026-11-26T10:00:00Z"}])
        self.assertEqual(answer["nextQuietAt"], "2026-11-26T10:38:00Z")

    def test_a_run_about_to_start_holds_it_too(self):
        rows = [row("borehole", 12, 0)]
        self.assertFalse(release_window.window(rows, dt.datetime(2026, 11, 26, 9, 45, tzinfo=UTC))["quiet"])
        self.assertTrue(release_window.window(rows, dt.datetime(2026, 11, 26, 9, 30, tzinfo=UTC))["quiet"])

    def test_a_campaign_switched_off_is_not_in_the_way(self):
        rows = [row("borehole", 12, 0, enabled=False)]
        self.assertTrue(release_window.window(rows, dt.datetime(2026, 11, 26, 10, 10, tzinfo=UTC))["quiet"])

    def test_the_gate_and_the_bot_share_one_arithmetic(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "promo_deploy_window", Path(__file__).resolve().parent / "promo-deploy-window.py")
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        for name in ("daily_starts", "runs_near", "conflicts", "next_quiet", "run_budget_minutes"):
            self.assertIs(getattr(gate, name), getattr(release_window, name))


if __name__ == "__main__":
    unittest.main()
