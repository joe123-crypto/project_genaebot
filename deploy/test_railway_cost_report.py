import copy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location("cost_report", Path(__file__).with_name("railway-cost-report.py"))
COST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COST)


class CostReportTests(unittest.TestCase):
    def snapshot(self, captured, spend):
        return {"scope": {"project": "fixture"}, "capturedAt": captured,
                "usage": {"billingPeriod": {"start": "2026-10-20T00:00:00+00:00", "end": "2026-11-20T00:00:00+00:00"},
                          "currentUsageDollars": spend, "estimatedBillDollars": 12,
                          "lineItems": [{"label": "Memory", "currentUsageDollars": spend}]}}

    def test_projection_uses_actual_cycle_length_and_does_not_replace_forecast(self):
        sample = self.snapshot("2026-10-27T00:00:00+00:00", 7)
        result = COST.projection(sample["usage"], datetime(2026, 10, 27, tzinfo=timezone.utc))
        self.assertEqual(result["linearResourceCycleDollars"], 31)
        self.assertEqual(sample["usage"]["estimatedBillDollars"], 12)
        self.assertIn("excludes", result["assumption"])

    def test_out_of_cycle_capture_never_extrapolates(self):
        usage = self.snapshot("unused", 7)["usage"]
        self.assertIsNone(COST.projection(usage, datetime(2026, 11, 21, tzinfo=timezone.utc))["linearResourceCycleDollars"])

    def test_same_cycle_delta_has_explicit_resource_units(self):
        before = self.snapshot("2026-10-21T00:00:00+00:00", 1)
        after = self.snapshot("2026-10-28T00:00:00+00:00", 8)
        result = COST.compare(before, after)
        self.assertEqual(result["resourceDollarsPerDay"], 1)
        self.assertEqual(result["byResourceDeltaDollars"]["Memory"], 7)

    def test_different_cycles_scopes_and_negative_adjustments_are_inconclusive(self):
        before = self.snapshot("2026-10-21T00:00:00+00:00", 1)
        after = self.snapshot("2026-10-28T00:00:00+00:00", 8)
        for change in ["cycle", "scope", "adjustment", "time"]:
            value = copy.deepcopy(after)
            if change == "cycle": value["usage"]["billingPeriod"]["start"] = "2026-11-20T00:00:00+00:00"
            if change == "scope": value["scope"]["project"] = "other"
            if change == "adjustment": value["usage"]["currentUsageDollars"] = 0
            if change == "time": value["capturedAt"] = before["capturedAt"]
            self.assertFalse(COST.compare(before, value)["comparable"])


if __name__ == "__main__":
    unittest.main()
