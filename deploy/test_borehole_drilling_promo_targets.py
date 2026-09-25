import json
import re
import sys
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TARGETS = ROOT / "workspace/cron/borehole-drilling-promo/targets.json"
LIB_DIR = ROOT / "workspace/lib"

# WhatsApp group JID: a numeric id suffixed with @g.us. The daily borehole ad only
# delivers when every target is a group the bot is a member of; invite links are
# not resolvable on this gateway, so the list must be JIDs.
GROUP_JID = re.compile(r"^\d+@g\.us$")

# MUTANDIRI VILLAGE RESIDENTS gets the borehole ad only three times a week
# (Mon/Wed/Fri), so it lives under weekly_groups rather than the daily list.
MUTANDIRI_JID = "120363166672509962@g.us"

if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))
import promo_broadcast  # noqa: E402


class BoreholePromoTargetsTests(unittest.TestCase):
    def test_targets_file_parses(self) -> None:
        self.assertTrue(TARGETS.is_file(), f"missing borehole targets file: {TARGETS}")
        data = json.loads(TARGETS.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)
        self.assertIn("groups", data)

    def test_groups_are_a_non_empty_list_of_valid_jids(self) -> None:
        data = json.loads(TARGETS.read_text(encoding="utf-8"))
        groups = data["groups"]
        self.assertIsInstance(groups, list, "groups must be a list")
        self.assertGreater(len(groups), 0, "groups must not be empty")
        for entry in groups:
            self.assertIsInstance(entry, str, f"group entry is not a string: {entry!r}")
            self.assertRegex(
                entry,
                GROUP_JID,
                f"group is not a valid WhatsApp group JID (expected <digits>@g.us): {entry!r}",
            )

    def test_groups_are_unique(self) -> None:
        data = json.loads(TARGETS.read_text(encoding="utf-8"))
        groups = data["groups"]
        duplicates = sorted({g for g in groups if groups.count(g) > 1})
        self.assertEqual(duplicates, [], f"duplicate borehole target groups: {duplicates}")


class BoreholePromoWeeklyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = json.loads(TARGETS.read_text(encoding="utf-8"))

    def test_weekly_groups_is_a_mapping(self) -> None:
        weekly = self.data.get("weekly_groups", {})
        self.assertIsInstance(weekly, dict, "weekly_groups must be an object")

    def test_weekly_group_keys_are_valid_jids(self) -> None:
        for jid in self.data.get("weekly_groups", {}):
            self.assertRegex(jid, GROUP_JID, f"weekly group is not a JID: {jid!r}")

    def test_mutandiri_is_weekly_not_daily(self) -> None:
        weekly = self.data.get("weekly_groups", {})
        self.assertIn(
            MUTANDIRI_JID, weekly,
            "MUTANDIRI must be configured as a weekly (3x/week) group",
        )
        self.assertNotIn(
            MUTANDIRI_JID, self.data.get("groups", []),
            "MUTANDIRI must not also be in the daily groups list, or it would post "
            "every day instead of three times a week",
        )

    def test_mutandiri_posts_exactly_three_times_a_week(self) -> None:
        weekly = promo_broadcast.load_weekly_targets(str(TARGETS))
        self.assertIn(MUTANDIRI_JID, weekly)
        self.assertEqual(
            len(weekly[MUTANDIRI_JID]), 3,
            "MUTANDIRI must be scheduled for exactly three weekdays (Mon/Wed/Fri)",
        )
        # Mon=0, Wed=2, Fri=4
        self.assertEqual(weekly[MUTANDIRI_JID], frozenset({0, 2, 4}))


if __name__ == "__main__":
    unittest.main()
