"""What clearing a group's local records does, and what it leaves alone.

The second half matters as much as the first. ``ad-refs.json`` and
``ad-ref-history.json`` are keyed by a ref code derived from the jid, are
written live by another process, and are evidence that somebody replied to an
advert. These pin that a clear never touches them, so a future change cannot
quietly widen "forget this group" into "erase its reply history".

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes them.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

LIB = str(Path(__file__).resolve().parent)
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import group_cleanup  # noqa: E402

JID = "263786627810-1599925488@g.us"
OTHER = "120363414718035443@g.us"


class GroupCleanupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"OPENCLAW_STATE_DIR": str(self.root)}
        self.states = self.root / "runtime" / "cron-state"

    def campaign(self, name, state):
        path = self.states / name / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
        return path

    def read(self, name):
        return json.loads((self.states / name / "state.json").read_text())

    def test_removes_the_group_from_every_campaign_that_posted_to_it(self):
        self.campaign("cv-promo", {"sent": {JID: "2026-08-20", OTHER: "2026-08-21"},
                                   "resolved": {}})
        self.campaign("genaie-promo", {"sent": {JID: "2026-09-01"},
                                       "resolved": {JID: "x", OTHER: "y"}})

        result = group_cleanup.clear_group(JID, env=self.env)

        self.assertEqual(result["campaigns"],
                         {"cv-promo": ["sent"], "genaie-promo": ["sent", "resolved"]})
        self.assertEqual(result["checked"], 2)
        self.assertEqual(self.read("cv-promo")["sent"], {OTHER: "2026-08-21"})
        self.assertEqual(self.read("genaie-promo"), {"sent": {}, "resolved": {OTHER: "y"}})

    def test_a_campaign_that_never_posted_to_it_is_left_untouched(self):
        path = self.campaign("cv-promo", {"sent": {OTHER: "2026-08-21"}, "resolved": {}})
        before = path.read_text()

        result = group_cleanup.clear_group(JID, env=self.env)

        self.assertEqual(result["campaigns"], {})
        self.assertEqual(result["checked"], 1)
        # Rewriting it would change its mtime for no reason, and a state file's
        # mtime is the only clue a run was here.
        self.assertEqual(path.read_text(), before)

    def test_reply_evidence_is_never_touched(self):
        self.campaign("cv-promo", {"sent": {JID: "2026-08-20"}, "resolved": {}})
        refs = self.root / "runtime" / "public-service-gate" / "ad-refs.json"
        history = self.root / "runtime" / "promo-caps" / "ad-ref-history.json"
        for path in (refs, history):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"CV1": {"uniqueSenders": 3}}))

        group_cleanup.clear_group(JID, env=self.env)

        for path in (refs, history):
            self.assertEqual(json.loads(path.read_text()), {"CV1": {"uniqueSenders": 3}})

    def test_an_unreadable_state_file_is_skipped_rather_than_failing_the_clear(self):
        self.campaign("good-promo", {"sent": {JID: "2026-08-20"}, "resolved": {}})
        broken = self.states / "broken-promo" / "state.json"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("{not json")

        result = group_cleanup.clear_group(JID, env=self.env)

        self.assertEqual(result["campaigns"], {"good-promo": ["sent"]})
        self.assertEqual(result["checked"], 2)
        self.assertEqual(broken.read_text(), "{not json")

    def test_a_state_file_whose_maps_are_the_wrong_shape_is_left_alone(self):
        self.campaign("odd-promo", {"sent": [JID], "resolved": None})

        result = group_cleanup.clear_group(JID, env=self.env)

        self.assertEqual(result["campaigns"], {})
        self.assertEqual(self.read("odd-promo"), {"sent": [JID], "resolved": None})

    def test_no_cron_state_directory_at_all_is_not_an_error(self):
        self.assertEqual(group_cleanup.clear_group(JID, env=self.env),
                         {"campaigns": {}, "checked": 0})

    def test_a_directory_without_a_state_file_is_not_counted(self):
        (self.states / "half-made").mkdir(parents=True)
        self.assertEqual(group_cleanup.clear_group(JID, env=self.env),
                         {"campaigns": {}, "checked": 0})

    def test_the_jid_has_to_be_one(self):
        with self.assertRaises(ValueError):
            group_cleanup.clear_group("", env=self.env)
        with self.assertRaises(ValueError):
            group_cleanup.clear_group(None, env=self.env)

    def test_the_state_directory_follows_the_volume(self):
        self.assertEqual(group_cleanup.cron_state_dir({"OPENCLAW_STATE_DIR": "/vol"}),
                         os.path.join("/vol", "runtime", "cron-state"))
        self.assertEqual(group_cleanup.cron_state_dir({}),
                         os.path.join(group_cleanup.DEFAULT_STATE_DIR, "runtime", "cron-state"))


if __name__ == "__main__":
    unittest.main()
