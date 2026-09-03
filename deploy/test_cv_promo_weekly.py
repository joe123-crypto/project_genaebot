import importlib.util
import io
import json
import re
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
CRON_DIR = ROOT / "workspace/cron/cv-promo"
TARGETS = CRON_DIR / "targets.json"
MODULE = CRON_DIR / "post_cv_ad.py"

GROUP_JID = re.compile(r"^\d+@g\.us$")

# The Vacancy Lamp group. The CV-making ad should reach it twice a week (not
# daily), so it lives under weekly_groups rather than the daily groups list.
VACANCY_LAMP_JID = "120363407177302513@g.us"


def load_module():
    spec = importlib.util.spec_from_file_location("post_cv_ad", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeDate(date):
    """A date subclass whose today() is pinned, so weekday gating is testable."""

    _pinned = date(2024, 1, 1)  # a Monday

    @classmethod
    def today(cls):
        return cls._pinned


class WeeklyConfigTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(TARGETS.read_text(encoding="utf-8"))

    def test_weekly_groups_is_a_mapping(self):
        weekly = self.data.get("weekly_groups", {})
        self.assertIsInstance(weekly, dict, "weekly_groups must be an object")

    def test_weekly_group_keys_are_valid_jids(self):
        for jid in self.data.get("weekly_groups", {}):
            self.assertRegex(jid, GROUP_JID, f"weekly group is not a JID: {jid!r}")

    def test_vacancy_lamp_is_weekly_not_daily(self):
        weekly = self.data.get("weekly_groups", {})
        self.assertIn(
            VACANCY_LAMP_JID, weekly,
            "Vacancy Lamp must be configured as a weekly (twice-a-week) group",
        )
        self.assertNotIn(
            VACANCY_LAMP_JID, self.data.get("groups", []),
            "Vacancy Lamp must not also be in the daily groups list, or it would "
            "post every day instead of twice a week",
        )

    def test_vacancy_lamp_posts_exactly_twice_a_week(self):
        module = load_module()
        weekly = module.load_weekly_targets()
        self.assertIn(VACANCY_LAMP_JID, weekly)
        self.assertEqual(
            len(weekly[VACANCY_LAMP_JID]), 2,
            "Vacancy Lamp must be scheduled for exactly two weekdays (twice a week)",
        )


class WeekdayParsingTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_parse_weekday_names_and_ints(self):
        p = self.module._parse_weekday
        self.assertEqual(p("mon"), 0)
        self.assertEqual(p("Monday"), 0)
        self.assertEqual(p("THU"), 3)
        self.assertEqual(p(3), 3)
        self.assertEqual(p("6"), 6)

    def test_parse_weekday_rejects_bad_tokens(self):
        p = self.module._parse_weekday
        self.assertIsNone(p("someday"))
        self.assertIsNone(p(9))
        self.assertIsNone(p(-1))
        self.assertIsNone(p(True))  # bool must not be read as int 1


class WeekdayGatingTests(unittest.TestCase):
    """Drive main() in --dry-run to confirm the weekly group is only eligible on
    its configured weekdays while daily groups are eligible every day."""

    def _run_dry_on(self, weekday_date):
        module = load_module()
        module.date = type(
            "PinnedDate", (_FakeDate,), {"_pinned": weekday_date}
        )
        old_argv = module.sys.argv
        module.sys.argv = ["post_cv_ad.py", "--dry-run"]
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                rc = module.main()
        finally:
            module.sys.argv = old_argv
        return rc, buf.getvalue()

    def test_weekly_group_skipped_off_day(self):
        # Tuesday (weekday 1) is not in {Mon, Thu}: Vacancy Lamp is skipped.
        rc, out = self._run_dry_on(date(2024, 1, 2))
        self.assertEqual(rc, 0)
        line = next(l for l in out.splitlines() if VACANCY_LAMP_JID in l)
        self.assertIn("skipped today", line)

    def test_weekly_group_sends_on_configured_day(self):
        # Monday (weekday 0) is in {Mon, Thu}: Vacancy Lamp would send.
        rc, out = self._run_dry_on(date(2024, 1, 1))
        self.assertEqual(rc, 0)
        line = next(l for l in out.splitlines() if VACANCY_LAMP_JID in l)
        self.assertIn("would send", line)
        self.assertNotIn("skipped today", line)


if __name__ == "__main__":
    unittest.main()
