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
import threading
import unittest
from pathlib import Path

LIB = str(Path(__file__).resolve().parent)
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import group_cleanup  # noqa: E402
import json_store  # noqa: E402

JID = "263786627810-1599925488@g.us"
OTHER = "120363414718035443@g.us"
# `resolved` caches invite-link lookups keyed by the LINK, with the jid as the
# value -- the opposite way round from `sent`.
INVITE = "https://chat.whatsapp.com/AbCdEf123456"
OTHER_INVITE = "https://chat.whatsapp.com/ZyXwVu987654"


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
                                       "resolved": {INVITE: JID, OTHER_INVITE: OTHER}})

        result = group_cleanup.clear_group(JID, env=self.env)

        self.assertEqual(result["campaigns"],
                         {"cv-promo": ["sent"], "genaie-promo": ["sent", "resolved"]})
        self.assertEqual(result["checked"], 2)
        self.assertEqual(self.read("cv-promo")["sent"], {OTHER: "2026-08-21"})
        self.assertEqual(self.read("genaie-promo"),
                         {"sent": {}, "resolved": {OTHER_INVITE: OTHER}})

    def test_an_invite_cache_is_cleared_by_the_jid_it_resolves_to(self):
        # `resolved` is keyed by the invite link, not the jid. Looking the jid up
        # as a KEY finds nothing, so the cache would survive and hand a later run
        # the group the operator asked to forget.
        self.campaign("cv-promo", {"sent": {}, "resolved": {INVITE: JID}})

        result = group_cleanup.clear_group(JID, env=self.env)

        self.assertEqual(result["campaigns"], {"cv-promo": ["resolved"]})
        self.assertEqual(self.read("cv-promo")["resolved"], {})

    def test_every_invite_that_resolves_to_the_group_goes(self):
        # One group can be reached by more than one link.
        self.campaign("cv-promo", {"sent": {},
                                   "resolved": {INVITE: JID, OTHER_INVITE: JID}})

        group_cleanup.clear_group(JID, env=self.env)

        self.assertEqual(self.read("cv-promo")["resolved"], {})

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

    def test_a_send_committed_while_the_sweep_waits_is_not_lost(self):
        """The failure this guards: a duplicate advert.

        ``promo_broadcast`` saves the whole state file after every send. If this
        sweep read the file, a send committed, and then the sweep wrote back
        what it had read, that send's receipt would vanish and the next run
        would post the same advert to that group again. Both sides take the
        file's lock, and the sweep reads INSIDE it, so the receipt survives.
        """
        path = self.campaign("cv-promo", {"sent": {JID: "2026-08-20"}, "resolved": {}})
        done = threading.Event()

        with json_store.file_lock(str(path)) as locked:
            self.assertTrue(locked, "this platform must support the lock")

            def sweep():
                group_cleanup.clear_group(JID, env=self.env)
                done.set()

            worker = threading.Thread(target=sweep)
            worker.start()
            # The sweep is now blocked on the lock. Commit a send for another
            # group, exactly as a broadcast run would.
            self.assertFalse(done.wait(0.3), "the sweep ran without the lock")
            json_store.write_json_atomic(str(path),
                {"sent": {JID: "2026-08-20", OTHER: "2026-11-09"}, "resolved": {}})

        worker.join(5)
        self.assertTrue(done.is_set())
        after = self.read("cv-promo")
        self.assertEqual(after["sent"], {OTHER: "2026-11-09"},
                         "the concurrent send receipt was overwritten")

    def test_a_lock_that_cannot_be_taken_never_stops_the_work(self):
        # Failing open matters most on the other side of this lock: a send must
        # never fail because a lock was unavailable.
        self.campaign("cv-promo", {"sent": {JID: "2026-08-20"}, "resolved": {}})
        original = json_store.fcntl
        json_store.fcntl = None
        try:
            result = group_cleanup.clear_group(JID, env=self.env)
        finally:
            json_store.fcntl = original
        self.assertEqual(result["campaigns"], {"cv-promo": ["sent"]})
        self.assertEqual(self.read("cv-promo")["sent"], {})

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
