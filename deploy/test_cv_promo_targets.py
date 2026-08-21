import json
import re
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TARGETS = ROOT / "workspace/cron/cv-promo/targets.json"

# WhatsApp group JID: a numeric id suffixed with @g.us. The daily CV-promo ad
# only delivers when every target is a group the bot is a member of; invite
# links are not resolvable on this gateway, so the list must be JIDs.
GROUP_JID = re.compile(r"^\d+@g\.us$")


class CvPromoTargetsTests(unittest.TestCase):
    def test_targets_file_parses(self) -> None:
        self.assertTrue(TARGETS.is_file(), f"missing CV-promo targets file: {TARGETS}")
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
        self.assertEqual(duplicates, [], f"duplicate CV-promo target groups: {duplicates}")


if __name__ == "__main__":
    unittest.main()
