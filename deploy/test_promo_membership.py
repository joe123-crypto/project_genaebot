"""workspace/cron/membership.json is the single source of truth for the groups
the bot advertises in (workspace/lib/promo_membership.py reads it).

The seven promo targets.json files used to be hand-maintained, with the same
JIDs copy-pasted across them and the exclusion rules repeated in prose. They are
now generated from membership.json. These tests pin the migration -- each
promo's group set and weekly cadence is exactly what the hand-written files
carried when they were folded in -- and the validation that keeps a bad edit
from reaching production.

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
CRON = ROOT / "workspace/cron"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import promo_membership as pm  # noqa: E402

MEMBERSHIP = CRON / "membership.json"

# Frozen from the hand-written targets.json files at the commit that introduced
# membership.json (#111's successor). A change here means a campaign's reach
# changed: intended edits update these numbers in the same commit, accidents
# show up as a failure.
EXPECTED_DAILY = {
    "appliance-repairs": 6,
    # big-player (2026-10-24) is the operator's borehole/solar lead-gen ad on the
    # second ("ads") WhatsApp line. Both ads campaigns now include Bulawayo
    # business group alongside REACH BYO MARKET 5 and BYO ADVERTISING.
    "big-player": 3,
    "industrial-lighting": 3,
    "borehole-drilling": 8,
    # borehole-paid (2026-10-23) is a PAID third-party borehole ad posted to the
    # same 23-group cohort as skylight-solar (identified by sharing its promo), so
    # it launches at skylight-solar's count. Separate from borehole-drilling above.
    "borehole-paid": 23,
    "building-materials": 10,
    # skylight-solar launched (2026-10-19) targeting the Bulawayo/BYO groups plus
    # Kwekwe, Gweru, both lowveld/Chiredzi groups, Mutare, Magwegwe, Lobengula West
    # and Chikombedzi: 23 groups, no weekly cadence.
    "skylight-solar": 23,
    # cv/fruit-trees/genaie/genaie-leads each gained the same five groups --
    # Business in Mutare, CHIKOMBEDZI MARKETPLACE, Gweru Market Mall, KWEKWE
    # BUSINESS PLUG 4 and LOWVELD HUSTLERS (#115): cv 18 -> 23, fruit-trees
    # 17 -> 22, genaie-leads 20 -> 25, genaie 21 -> 26.
    # Then each gained the same ten newly-joined groups discovered in the
    # sender-key store (one in-roster with empty promos, nine new, seven of
    # them legacy <creator>-<timestamp> JIDs from the 2026-10-17 burst): cv
    # 23 -> 33, fruit-trees 22 -> 32, genaie-leads 25 -> 35, genaie 26 -> 36.
    # Then three groups started answering every send with 'forbidden' and were
    # marked admin-only: ZIMBABWE UPDATE BYO (fruit-trees, cv, genaie,
    # genaie-leads), 120363168421689199 (cv, genaie, genaie-leads) and
    # 263713652561-1592027591 (cv, fruit-trees, genaie, genaie-leads): cv
    # 33 -> 30, fruit-trees 32 -> 30, genaie-leads 35 -> 32, genaie 36 -> 33.
    # Those three admin-only groups then had their promo lists cleared to [] on
    # 2026-10-18 (terminal decommission); the counts above are unchanged because
    # admin-only had already excluded them.
    # Then each gained the same fourteen groups from the 2026-10-18 Bulawayo
    # burst (fifteen new joins named that day, all except Harare business
    # classifie, all legacy <creator>-<timestamp> JIDs): cv 30 -> 44, fruit-trees
    # 30 -> 44, genaie-leads 32 -> 46, genaie 33 -> 47. The shared promo cron
    # timeout was raised 5400 -> 7200 s to hold promos up to ~60 groups.
    # (Harare business classifie instead took appliance-repairs: 5 -> 6.)
    "cv": 44,
    "fruit-trees": 44,
    "genaie-leads": 46,
    "genaie": 47,
}
EXPECTED_WEEKLY = {
    "borehole-drilling": {"120363166672509962@g.us": ["mon", "wed", "fri"]},
    "building-materials": {"120363166672509962@g.us": ["mon", "wed", "fri"]},
    "cv": {"120363407177302513@g.us": ["mon", "thu"]},
}


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


class MembershipFileTests(unittest.TestCase):
    def setUp(self):
        self.m = pm.load(str(MEMBERSHIP))
        self.assertIsNotNone(self.m, "membership.json must exist and parse")

    def test_the_committed_file_is_valid(self):
        self.assertEqual(pm.validate(self.m), [])

    def test_declares_exactly_the_promo_directories(self):
        on_disk = sorted(p.name[: -len("-promo")] for p in CRON.glob("*-promo"))
        self.assertEqual(sorted(self.m["promos"]), on_disk)

    def test_every_promo_has_a_generated_file_comment(self):
        for promo, meta in self.m["promos"].items():
            self.assertIn("GENERATED", meta.get("comment", ""),
                          "%s's targets.json must say it is generated" % promo)

    def test_group_counts_match_the_hand_written_targets_files(self):
        for promo, expected in EXPECTED_DAILY.items():
            jids, _ = pm.groups_for_promo(self.m, promo)
            self.assertEqual(len(jids), expected, promo)
            self.assertEqual(len(set(jids)), len(jids), "%s has duplicates" % promo)

    def test_weekly_cadences_match_the_hand_written_targets_files(self):
        for promo in self.m["promos"]:
            _, weekly = pm.groups_for_promo(self.m, promo)
            self.assertEqual(weekly, EXPECTED_WEEKLY.get(promo, {}), promo)

    def test_a_weekly_group_is_not_also_in_the_daily_list(self):
        for promo in self.m["promos"]:
            jids, weekly = pm.groups_for_promo(self.m, promo)
            self.assertEqual(set(jids) & set(weekly), set(), promo)

    def test_only_active_groups_are_advertised_to(self):
        for jid, entry in pm.groups(self.m).items():
            if pm.status_of(entry) != "active":
                self.assertEqual(pm.promos_for_group(self.m, jid), [],
                                 "%s is %s but still advertised to"
                                 % (jid, pm.status_of(entry)))

    def test_paused_groups_keep_their_record(self):
        """admin-only / not-a-member / on-hold groups stay listed, so the
        discovery sweep does not re-add them as if they were new. At least one
        non-active (paused) record must survive; which paused statuses are
        present varies as groups are removed or resumed."""
        statuses = set(pm.status_of(e) for e in pm.groups(self.m).values())
        self.assertIn("admin-only", statuses)
        self.assertIn("not-a-member", statuses)
        self.assertTrue(statuses - {"active"},
                        "at least one paused (non-active) record must survive")

    def test_caps_are_off_in_the_committed_file(self):
        """Rollout phase A ships the machinery dark; flipping this is phase B."""
        self.assertFalse(pm.caps_enabled(self.m))

    def test_every_jid_is_a_group_jid(self):
        for jid in pm.groups(self.m):
            self.assertTrue(jid.endswith("@g.us"), jid)

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

    def test_load_returns_none_for_a_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(pm.load(str(Path(tmp, "nope.json"))))

    def test_load_raises_on_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp, "membership.json")
            bad.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                pm.load(str(bad))


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
