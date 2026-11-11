"""The shape of the operator config, and the validation that keeps a bad edit
out of production (workspace/lib/promo_membership.py).

This file used to carry the ad roster: every campaign's group count and weekly
cadence, frozen at the commit that folded the hand-written targets files
together, with a running commentary of which groups had joined which campaign
and when. That made it a second place the operator's data lived, and a test that
failed because someone paused a group. The config is in Firestore now, so the
roster is gone from here and what is left is the rules -- checked against
``deploy/promo_fixture``, which is built to exercise each one.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes them.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = ROOT / "workspace/lib"

if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

if str(ROOT / "deploy") not in sys.path:
    sys.path.insert(0, str(ROOT / "deploy"))

import promo_membership as pm  # noqa: E402
import promo_fixture as fixture  # noqa: E402



def minimal(**groups):
    """A tiny valid membership doc, for validation tests."""
    return {
        "version": 1,
        "defaults": {"activity": "normal",
                     "bands": {"busy": [6, 10], "normal": [4, 6], "quiet": [1, 3]}},
        "promos": {"cv": {}, "genaie": {}},
        "groups": groups,
    }


class ContactAccountTests(unittest.TestCase):
    def test_contact_is_generated_without_changing_account_numbers(self):
        m = minimal()
        m["defaults"].update({
            "accounts": {"default": {"number": "+263785632943"},
                         "ads": {"number": "+263790453292"}},
            "contact_account": "ads",
        })
        self.assertEqual(pm.validate(m), [])
        payload = pm.targets_payload(m, "cv")
        self.assertEqual(payload["contact_account"], "ads")
        self.assertEqual(payload["account_numbers"]["default"], "+263785632943")

    def test_invalid_contact_account_is_rejected(self):
        for contact in ("missing", "", [], 42):
            with self.subTest(contact=contact):
                m = minimal()
                m["defaults"]["contact_account"] = contact
                self.assertTrue(any("contact_account" in e for e in pm.validate(m)))

    def test_contact_account_requires_number(self):
        m = minimal()
        m["defaults"].update({"contact_account": "ads", "accounts": {"ads": {}}})
        self.assertTrue(any("contact_account" in e for e in pm.validate(m)))

    def test_absent_contact_preserves_legacy_payload(self):
        self.assertNotIn("contact_account", pm.targets_payload(minimal(), "cv"))


class ConfigShapeTests(unittest.TestCase):
    """The rules a config has to satisfy, asserted against the shared fixture.

    These used to be asserted against the committed membership.json, together
    with its group counts and weekly cadences frozen into this file. That made
    the test a second home for the ad roster: a test could start failing because
    someone paused a group. The config lives in Firestore now, so what is left
    here is the shape every config must have, checked against
    ``deploy/promo_fixture``, which is deliberately built to exercise each rule.
    """

    def setUp(self):
        self.m = fixture.membership()

    def test_the_fixture_is_a_valid_config(self):
        self.assertEqual(pm.validate(self.m), [])

    def test_every_jid_is_a_group_jid(self):
        for jid in pm.groups(self.m):
            self.assertTrue(jid.endswith("@g.us"), jid)

    def test_a_weekly_group_is_not_also_in_the_daily_list(self):
        for promo in self.m["promos"]:
            jids, weekly = pm.groups_for_promo(self.m, promo)
            self.assertEqual(set(jids) & set(weekly), set(), promo)
        # And the fixture has at least one, or the rule is untested.
        self.assertTrue(any(pm.groups_for_promo(self.m, promo)[1]
                            for promo in self.m["promos"]))

    def test_only_active_groups_are_advertised_to(self):
        # A non-active group keeps its configured campaigns -- that is how
        # resuming it is one status flip -- but must reach no target list.
        targeted = set()
        for promo in self.m["promos"]:
            daily, weekly = pm.groups_for_promo(self.m, promo)
            targeted.update(daily)
            targeted.update(weekly)
        for jid, entry in pm.groups(self.m).items():
            if pm.status_of(entry) != "active":
                self.assertNotIn(jid, targeted,
                                 "%s is %s but still advertised to"
                                 % (jid, pm.status_of(entry)))
                self.assertTrue(pm.promos_for_group(self.m, jid) is not None)

    def test_a_paused_record_keeps_its_place(self):
        # A non-active record stays listed, so the discovery sweep does not
        # re-add the group as if it were new.
        statuses = set(pm.status_of(e) for e in pm.groups(self.m).values())
        self.assertTrue(statuses - {"active"},
                        "the fixture must carry at least one paused record")

    def test_bands_cover_every_tier_in_use(self):
        bands = pm.bands(self.m)
        for jid, entry in pm.groups(self.m).items():
            self.assertIn(pm.activity_of(self.m, entry), bands, jid)


class ProjectionTests(unittest.TestCase):
    def test_groups_for_promo_preserves_membership_order(self):
        m = minimal(**{
            "1@g.us": {"promos": ["cv"]},
            "2@g.us": {"promos": ["genaie"]},
            "3@g.us": {"promos": ["cv"]},
        })
        self.assertEqual(pm.groups_for_promo(m, "cv"), (["1@g.us", "3@g.us"], {}))

    def test_weekly_group_is_split_out(self):
        m = minimal(**{"1@g.us": {"promos": ["cv"], "weekly": {"cv": ["mon"]}}})
        self.assertEqual(pm.groups_for_promo(m, "cv"), ([], {"1@g.us": ["mon"]}))

    def test_status_gates_generation(self):
        m = minimal(**{"1@g.us": {"promos": ["cv"], "status": "on-hold"}})
        self.assertEqual(pm.groups_for_promo(m, "cv"), ([], {}))
        self.assertEqual(pm.promos_for_group(m, "1@g.us"), [])

    def test_a_paused_campaign_skips_the_group_and_only_that_campaign(self):
        m = minimal(**{"1@g.us": {"promos": ["cv", "genaie"], "paused_promos": ["cv"]}})
        self.assertEqual(pm.groups_for_promo(m, "cv"), ([], {}))
        self.assertEqual(pm.groups_for_promo(m, "genaie"), (["1@g.us"], {}))

    def test_a_paused_campaign_takes_none_of_the_groups_cap_turns(self):
        # promos_for_group is what promo_caps rotates the daily allowance over,
        # so a campaign that cannot post must not be handed a turn.
        m = minimal(**{"1@g.us": {"promos": ["cv", "genaie"], "paused_promos": ["cv"]}})
        self.assertEqual(pm.promos_for_group(m, "1@g.us"), ["genaie"])

    def test_a_paused_weekly_campaign_is_out_of_the_weekly_list_too(self):
        m = minimal(**{"1@g.us": {"promos": ["cv"], "weekly": {"cv": ["mon"]},
                                  "paused_promos": ["cv"]}})
        self.assertEqual(pm.groups_for_promo(m, "cv"), ([], {}))

    def test_pausing_keeps_the_assignment(self):
        # Resuming is a flip, not a rediscovery: the campaign stays in promos.
        m = minimal(**{"1@g.us": {"promos": ["cv"], "paused_promos": ["cv"]}})
        self.assertEqual(m["groups"]["1@g.us"]["promos"], ["cv"])
        del m["groups"]["1@g.us"]["paused_promos"]
        self.assertEqual(pm.groups_for_promo(m, "cv"), (["1@g.us"], {}))

    def test_targets_payload_omits_weekly_keys_when_there_are_none(self):
        m = minimal(**{"1@g.us": {"promos": ["cv"]}})
        m["promos"]["cv"] = {"comment": "c", "weekly_comment": "w"}
        payload = pm.targets_payload(m, "cv")
        self.assertEqual(list(payload), ["_comment", "groups"])

    def test_dumps_targets_matches_the_hand_written_formatting(self):
        """Two-space indent, UTF-8, trailing newline, and each weekly day list on
        one line -- byte-stability is what lets CI diff a regeneration."""
        payload = {"_comment": "c", "groups": ["1@g.us"],
                   "weekly_groups": {"2@g.us": ["mon", "thu"]}}
        text = pm.dumps_targets(payload)
        self.assertIn('"2@g.us": ["mon", "thu"]', text)
        self.assertIn('  "groups": [\n    "1@g.us"\n  ]', text)
        self.assertTrue(text.endswith("}\n"))
        self.assertEqual(json.loads(text), payload)

    def test_dumps_targets_keeps_non_ascii_names_readable(self):
        text = pm.dumps_targets({"_comment": "Bindura 💰", "groups": []})
        self.assertIn("💰", text)
        self.assertNotIn("\\u", text)


class ValidationTests(unittest.TestCase):
    def assertComplains(self, m, needle):
        errors = pm.validate(m)
        self.assertTrue(any(needle in e for e in errors),
                        "expected a complaint about %r, got %r" % (needle, errors))

    def test_unknown_status(self):
        self.assertComplains(minimal(**{"1@g.us": {"status": "paused"}}),
                             "unknown status")

    def test_unknown_promo(self):
        self.assertComplains(minimal(**{"1@g.us": {"promos": ["nope"]}}),
                             "unknown promo")

    def test_promo_used_but_not_declared(self):
        m = minimal(**{"1@g.us": {"promos": ["cv"]}})
        del m["promos"]["cv"]
        self.assertComplains(m, "not declared")

    def test_unknown_activity_tier(self):
        self.assertComplains(minimal(**{"1@g.us": {"activity": "frantic"}}),
                             "unknown activity tier")

    def test_bad_weekday_token(self):
        m = minimal(**{"1@g.us": {"promos": ["cv"], "weekly": {"cv": ["moonday"]}}})
        self.assertComplains(m, "invalid weekday")

    def test_weekly_for_a_promo_the_group_is_not_in(self):
        m = minimal(**{"1@g.us": {"promos": ["cv"], "weekly": {"genaie": ["mon"]}}})
        self.assertComplains(m, "not in its promos")

    def test_duplicate_promo_entries(self):
        self.assertComplains(minimal(**{"1@g.us": {"promos": ["cv", "cv"]}}),
                             "duplicates")

    def test_pausing_a_campaign_the_group_is_not_in(self):
        m = minimal(**{"1@g.us": {"promos": ["cv"], "paused_promos": ["genaie"]}})
        self.assertComplains(m, "paused_promos names promo 'genaie'")

    def test_paused_promos_must_be_a_list_of_names(self):
        self.assertComplains(minimal(**{"1@g.us": {"promos": ["cv"], "paused_promos": "cv"}}),
                             "paused_promos must be a list")
        self.assertComplains(minimal(**{"1@g.us": {"promos": ["cv"], "paused_promos": [1]}}),
                             "paused_promos must be a list")

    def test_duplicate_paused_entries(self):
        m = minimal(**{"1@g.us": {"promos": ["cv"], "paused_promos": ["cv", "cv"]}})
        self.assertComplains(m, "paused_promos has duplicates")

    def test_a_valid_pause_is_accepted(self):
        m = minimal(**{"1@g.us": {"promos": ["cv"], "paused_promos": ["cv"]}})
        self.assertEqual(pm.validate(m), [])

    def test_negative_daily_cap(self):
        self.assertComplains(minimal(**{"1@g.us": {"daily_cap": -1}}), "daily_cap")

    def test_duplicate_names_are_flagged(self):
        m = minimal(**{"1@g.us": {"name": "Same"}, "2@g.us": {"name": "Same"}})
        self.assertComplains(m, "is also used by")

    def test_non_group_jid(self):
        self.assertComplains(minimal(**{"263785811799@s.whatsapp.net": {}}),
                             "@g.us")

    def test_wrong_version(self):
        m = minimal()
        m["version"] = 2
        self.assertComplains(m, "unsupported version")

    def test_weekday_ints_are_accepted(self):
        m = minimal(**{"1@g.us": {"promos": ["cv"], "weekly": {"cv": [0, 3]}}})
        self.assertEqual(pm.validate(m), [])


if __name__ == "__main__":
    unittest.main()
