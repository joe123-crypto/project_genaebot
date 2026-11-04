"""Per-group daily ad caps and the rotation that allocates them
(workspace/lib/promo_caps.py).

A group in six campaigns used to take six ads a day whatever its appetite. Now
each group has a daily allowance -- the operator's activity tier sets the band,
the ad-reply counters the bot already records move the cap inside it -- and when
the allowance is smaller than the number of campaigns targeting the group, a
rotation decides whose turn it is.

The two properties that matter most are pinned here:

* the rotation is a PURE function of (day, jid, promo set, cap), so all seven
  crons reach the same answer in separate processes with no shared state -- which
  is what makes this safe given json_store is last-writer-wins; and
* over one full cycle every campaign gets exactly ``cap`` turns, so no campaign
  starves on a heavily-targeted group.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes them.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stderr
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = ROOT / "workspace/lib"
PLUGIN_AD_REFS = ROOT / "workspace/plugins/public-service-gate/lib/ad-refs.js"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import promo_broadcast as pb  # noqa: E402
import promo_caps as pc  # noqa: E402
import promo_membership as pm  # noqa: E402

DAY = date(2026, 10, 15)


def membership(groups, caps_enabled=True, bands=None, promos=("a", "b", "c", "d")):
    return {
        "version": 1,
        "defaults": {
            "activity": "normal",
            "caps_enabled": caps_enabled,
            "bands": bands or {"busy": [6, 10], "normal": [4, 6], "quiet": [1, 3]},
        },
        "promos": dict((p, {}) for p in promos),
        "groups": groups,
    }


class RotationTests(unittest.TestCase):
    PROMOS = ["borehole-drilling", "fruit-trees", "cv", "genaie", "genaie-leads"]
    JID = "120363292471414376@g.us"

    def test_cap_is_respected(self):
        for cap in range(0, len(self.PROMOS) + 2):
            got = pc.allowed_promos(self.JID, self.PROMOS, cap, day=DAY)
            self.assertEqual(len(got), min(cap, len(self.PROMOS)), cap)

    def test_cap_at_or_above_promo_count_allows_everything(self):
        self.assertEqual(pc.allowed_promos(self.JID, self.PROMOS, 5, day=DAY),
                         self.PROMOS)
        self.assertEqual(pc.allowed_promos(self.JID, self.PROMOS, 99, day=DAY),
                         self.PROMOS)

    def test_cap_zero_allows_nothing(self):
        self.assertEqual(pc.allowed_promos(self.JID, self.PROMOS, 0, day=DAY), [])

    def test_result_preserves_the_given_promo_order(self):
        got = pc.allowed_promos(self.JID, self.PROMOS, 3, day=DAY)
        self.assertEqual(got, [p for p in self.PROMOS if p in got])

    def test_same_day_same_answer(self):
        a = pc.allowed_promos(self.JID, self.PROMOS, 3, day=DAY)
        b = pc.allowed_promos(self.JID, self.PROMOS, 3, day=DAY)
        self.assertEqual(a, b)

    def test_identical_in_a_separate_process(self):
        """The whole no-shared-state design rests on this: a fresh interpreter,
        with its own hash seed, must choose the same campaigns."""
        code = (
            "import sys; sys.path.insert(0, %r);"
            "import promo_caps, json;"
            "print(json.dumps(promo_caps.allowed_promos(%r, %r, 3,"
            " day=__import__('datetime').date(2026, 10, 15))))"
            % (str(LIB_DIR), self.JID, self.PROMOS)
        )
        env = dict(os.environ, PYTHONHASHSEED="1")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, env=env, check=True).stdout
        self.assertEqual(json.loads(out),
                         pc.allowed_promos(self.JID, self.PROMOS, 3, day=DAY))

    def test_a_full_cycle_gives_every_promo_exactly_cap_turns(self):
        n = len(self.PROMOS)
        for cap in range(1, n):
            counts = Counter()
            for offset in range(n):
                for promo in pc.allowed_promos(self.JID, self.PROMOS, cap,
                                               day=DAY + timedelta(days=offset)):
                    counts[promo] += 1
            self.assertEqual(sorted(counts.values()), [cap] * n,
                             "cap %d is unfair: %r" % (cap, dict(counts)))

    def test_the_window_moves_day_to_day(self):
        seen = set()
        for offset in range(len(self.PROMOS)):
            seen.add(tuple(pc.allowed_promos(self.JID, self.PROMOS, 2,
                                             day=DAY + timedelta(days=offset))))
        self.assertGreater(len(seen), 1, "the allowance never rotates")

    def test_different_groups_get_different_rotations(self):
        other = "120363411788614990@g.us"
        same = sum(1 for offset in range(10)
                   if pc.allowed_promos(self.JID, self.PROMOS, 2,
                                        day=DAY + timedelta(days=offset))
                   == pc.allowed_promos(other, self.PROMOS, 2,
                                        day=DAY + timedelta(days=offset)))
        self.assertLess(same, 10, "every group rotates in lockstep")

    def test_rotation_order_is_a_permutation(self):
        order = pc.rotation_order(self.JID, self.PROMOS)
        self.assertEqual(sorted(order), sorted(self.PROMOS))


class CapTests(unittest.TestCase):
    def test_tier_band_floor_with_no_replies(self):
        m = membership({"1@g.us": {"promos": ["a"], "activity": "busy"}})
        self.assertEqual(pc.target_cap(m, "1@g.us", 0.0), 6)
        m = membership({"1@g.us": {"promos": ["a"], "activity": "quiet"}})
        self.assertEqual(pc.target_cap(m, "1@g.us", 0.0), 1)

    def test_replies_raise_the_cap_within_the_band(self):
        m = membership({"1@g.us": {"promos": ["a"], "activity": "quiet"}})
        self.assertEqual(pc.target_cap(m, "1@g.us", 1.0), 2)
        self.assertEqual(pc.target_cap(m, "1@g.us", 2.0), 3)

    def test_band_ceiling_clamps(self):
        m = membership({"1@g.us": {"promos": ["a"], "activity": "quiet"}})
        self.assertEqual(pc.target_cap(m, "1@g.us", 99.0), 3)
        m = membership({"1@g.us": {"promos": ["a"], "activity": "busy"}})
        self.assertEqual(pc.target_cap(m, "1@g.us", 99.0), 10)

    def test_default_tier_is_used_when_the_group_has_none(self):
        m = membership({"1@g.us": {"promos": ["a"]}})
        self.assertEqual(pc.target_cap(m, "1@g.us", 0.0), 4)

    def test_explicit_daily_cap_overrides_the_band(self):
        m = membership({"1@g.us": {"promos": ["a"], "activity": "busy",
                                   "daily_cap": 2}})
        self.assertEqual(pc.target_cap(m, "1@g.us", 99.0), 2)

    def test_zero_override_is_honoured(self):
        m = membership({"1@g.us": {"promos": ["a"], "daily_cap": 0}})
        self.assertEqual(pc.target_cap(m, "1@g.us", 99.0), 0)

    def test_non_active_group_gets_nothing(self):
        for status in ("on-hold", "admin-only", "not-a-member"):
            m = membership({"1@g.us": {"promos": ["a"], "status": status}})
            self.assertEqual(pc.target_cap(m, "1@g.us", 99.0), 0, status)

    def test_unknown_group_has_no_cap(self):
        self.assertIsNone(pc.target_cap(membership({}), "1@g.us", 0.0))

    def test_smoothing_limits_movement_to_one_a_day(self):
        self.assertEqual(pc.smooth_cap(10, 4), 5)
        self.assertEqual(pc.smooth_cap(1, 6), 5)
        self.assertEqual(pc.smooth_cap(5, 5), 5)
        self.assertEqual(pc.smooth_cap(6, 5), 6)

    def test_smoothing_is_skipped_without_history(self):
        self.assertEqual(pc.smooth_cap(10, None), 10)


class AdRefScoreTests(unittest.TestCase):
    JID = "120363275485319265@g.us"

    def refs_for(self, jid, prefix="BH", **fields):
        return {pb.ref_code(prefix, jid): fields}

    def test_score_uses_unique_senders(self):
        refs = self.refs_for(self.JID, uniqueSenders=3, count=9,
                             lastSeen=DAY.isoformat())
        self.assertEqual(pc.ad_ref_scores([self.JID], refs=refs, today=DAY),
                         {self.JID: 3.0})

    def test_a_single_persistent_replier_cannot_inflate_a_group(self):
        refs = self.refs_for(self.JID, uniqueSenders=1, count=50,
                             lastSeen=DAY.isoformat())
        self.assertEqual(pc.ad_ref_scores([self.JID], refs=refs, today=DAY),
                         {self.JID: 1.0})

    def test_replies_decay_with_age(self):
        for age, expected in ((0, 2.0), (7, 2.0), (10, 1.0), (20, 0.5), (90, 0.0)):
            refs = self.refs_for(self.JID, uniqueSenders=2,
                                 lastSeen=(DAY - timedelta(days=age)).isoformat())
            got = pc.ad_ref_scores([self.JID], refs=refs, today=DAY)[self.JID]
            self.assertEqual(got, expected, "age %d" % age)

    def test_scores_sum_across_promos_for_one_group(self):
        refs = {}
        refs.update(self.refs_for(self.JID, "BH", uniqueSenders=1,
                                  lastSeen=DAY.isoformat()))
        refs.update(self.refs_for(self.JID, "CV", uniqueSenders=2,
                                  lastSeen=DAY.isoformat()))
        self.assertEqual(pc.ad_ref_scores([self.JID], refs=refs, today=DAY),
                         {self.JID: 3.0})

    def test_unknown_group_scores_zero(self):
        self.assertEqual(pc.ad_ref_scores(["9@g.us"], refs={}, today=DAY),
                         {"9@g.us": 0.0})

    def test_missing_unique_senders_falls_back_to_count(self):
        refs = self.refs_for(self.JID, count=4, lastSeen=DAY.isoformat())
        self.assertEqual(pc.ad_ref_scores([self.JID], refs=refs, today=DAY),
                         {self.JID: 1.0})

    def test_entry_without_last_seen_scores_zero(self):
        refs = self.refs_for(self.JID, uniqueSenders=5)
        self.assertEqual(pc.ad_ref_scores([self.JID], refs=refs, today=DAY),
                         {self.JID: 0.0})

    def test_ref_codes_match_the_engine(self):
        """Inversion is by COMPUTING each group's codes, never by decoding, so
        it has to agree with promo_broadcast.ref_code exactly."""
        self.assertEqual(pc.ref_codes_for(self.JID),
                         [pb.ref_code(p, self.JID) for p in pc.AD_REF_PREFIXES])

    def test_prefixes_mirror_the_plugin_and_exclude_appliance_repairs(self):
        source = PLUGIN_AD_REFS.read_text(encoding="utf-8")
        for prefix in pc.AD_REF_PREFIXES:
            self.assertIn('"%s"' % prefix, source,
                          "%s is not a prefix the plugin records" % prefix)
        self.assertNotIn("AR", pc.AD_REF_PREFIXES,
                         "the appliance-repairs ad links to the shop's own "
                         "number, so the bot never sees those replies")

    def test_missing_ad_refs_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pc.load_ad_refs(str(Path(tmp, "nope.json"))), {})

    def test_malformed_ad_refs_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp, "ad-refs.json")
            bad.write_text("{{{", encoding="utf-8")
            self.assertEqual(pc.load_ad_refs(str(bad)), {})


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.m = membership({
            "1@g.us": {"promos": ["a", "b", "c", "d"], "activity": "quiet"},
            "2@g.us": {"promos": ["a", "b"], "activity": "busy"},
            "3@g.us": {"promos": ["a"], "status": "on-hold"},
        })

    def test_plan_caps_a_heavily_targeted_quiet_group(self):
        plan = pc.plan(self.m, ["1@g.us"], day=DAY, refs={})
        self.assertEqual(plan["1@g.us"]["cap"], 1)
        self.assertEqual(len(plan["1@g.us"]["allowed"]), 1)

    def test_plan_leaves_a_group_alone_when_its_cap_covers_every_promo(self):
        plan = pc.plan(self.m, ["2@g.us"], day=DAY, refs={})
        self.assertEqual(plan["2@g.us"]["allowed"], ["a", "b"])

    def test_plan_marks_unknown_groups_and_lets_them_through(self):
        plan = pc.plan(self.m, ["9@g.us"], day=DAY, refs={})
        self.assertFalse(plan["9@g.us"]["known"])
        self.assertIsNone(plan["9@g.us"]["allowed"])

    def test_filter_for_promo_drops_only_capped_groups(self):
        kept, decisions = pc.filter_for_promo(
            self.m, "a", ["1@g.us", "2@g.us", "9@g.us"], day=DAY, refs={})
        self.assertIn("2@g.us", kept)
        self.assertIn("9@g.us", kept, "an unrecorded group must not be dropped")
        self.assertEqual("1@g.us" in kept, "a" in decisions["1@g.us"]["allowed"])

    def test_no_group_exceeds_its_cap_across_all_promos_on_one_day(self):
        """The property the whole feature exists for, checked the way the seven
        crons actually behave: each promo filters independently."""
        jids = ["1@g.us", "2@g.us"]
        sends = Counter()
        for promo in ["a", "b", "c", "d"]:
            kept, decisions = pc.filter_for_promo(self.m, promo, jids, day=DAY,
                                                  refs={})
            for jid in kept:
                sends[jid] += 1
        for jid in jids:
            self.assertLessEqual(sends[jid], pc.plan(self.m, [jid], day=DAY,
                                                     refs={})[jid]["cap"], jid)

    def test_paused_group_is_filtered_out_entirely(self):
        kept, _ = pc.filter_for_promo(self.m, "a", ["3@g.us"], day=DAY, refs={})
        self.assertEqual(kept, [])


class DisabledCampaignTests(unittest.TestCase):
    """A campaign the cron manifest switches off never posts, so it must not
    take a group's daily turn (the CV pause of 2026-11-04 cost ~20 slots/day)."""

    def setUp(self):
        self.m = membership({
            "1@g.us": {"promos": ["a", "b", "c", "d"], "activity": "quiet"},
        }, bands={"busy": [6, 10], "normal": [4, 6], "quiet": [2, 2]})

    def manifest(self, rows):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        self.addCleanup(os.unlink, handle.name)
        json.dump({"version": 1, "promos": rows}, handle)
        handle.close()
        return handle.name

    def test_disabled_campaign_never_wins_a_turn(self):
        for offset in range(8):
            day = DAY + timedelta(days=offset)
            info = pc.plan(self.m, ["1@g.us"], day=day, refs={},
                           disabled={"a"})["1@g.us"]
            self.assertNotIn("a", info["promos"])
            self.assertNotIn("a", info["allowed"])
            self.assertEqual(len(info["allowed"]), 2, day)

    def test_running_campaigns_share_the_whole_cycle(self):
        turns = Counter()
        for offset in range(3):
            info = pc.plan(self.m, ["1@g.us"], day=DAY + timedelta(days=offset),
                           refs={}, disabled={"a"})["1@g.us"]
            turns.update(info["allowed"])
        self.assertEqual(turns, Counter({"b": 2, "c": 2, "d": 2}))

    def test_other_campaigns_skip_a_disabled_one_when_filtering(self):
        for offset in range(4):
            day = DAY + timedelta(days=offset)
            _, decisions = pc.filter_for_promo(self.m, "b", ["1@g.us"], day=day,
                                               refs={}, disabled={"a"})
            self.assertNotIn("a", decisions["1@g.us"]["allowed"])

    def test_a_manual_run_of_the_disabled_campaign_still_gets_its_rotation(self):
        """Running or dry-running a paused campaign by hand must behave as if it
        were switched on, not report every group as capped."""
        for offset in range(4):
            day = DAY + timedelta(days=offset)
            kept, _ = pc.filter_for_promo(self.m, "a", ["1@g.us"], day=day,
                                          refs={}, disabled={"a"})
            enabled_kept, _ = pc.filter_for_promo(self.m, "a", ["1@g.us"], day=day,
                                                  refs={}, disabled=set())
            self.assertEqual(kept, enabled_kept, day)

    def test_disabled_promos_reads_only_explicit_false(self):
        path = self.manifest([
            {"slug": "a", "enabled": False},
            {"slug": "b", "enabled": True},
            {"slug": "c"},
            {"slug": "d", "enabled": "false"},
        ])
        self.assertEqual(pc.disabled_promos([path]), {"a"})

    def test_disabled_promos_fails_open(self):
        missing = os.path.join(tempfile.gettempdir(), "no-such-promo-crons.json")
        self.assertEqual(pc.disabled_promos([missing]), set())
        bad = self.manifest([])
        with open(bad, "w") as handle:
            handle.write("{not json")
        self.assertEqual(pc.disabled_promos([bad]), set())

    def test_repo_manifest_is_found_and_matches_it(self):
        declared = json.loads((ROOT / "deploy/promo-crons.json").read_text())
        expected = set(row["slug"] for row in declared["promos"]
                       if row.get("enabled") is False)
        self.assertEqual(pc.disabled_promos(), expected)


class SnapshotTests(unittest.TestCase):
    JID = "120363275485319265@g.us"

    def test_snapshot_is_idempotent_per_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "history.json"))
            refs = {pb.ref_code("BH", self.JID): {"uniqueSenders": 1, "count": 1,
                                                  "lastSeen": DAY.isoformat()}}
            pc.snapshot_ad_refs(refs, today=DAY, path=path)
            pc.snapshot_ad_refs(refs, today=DAY, path=path)
            with open(path, encoding="utf-8") as f:
                history = json.load(f)
            self.assertEqual(list(history["days"]), [DAY.isoformat()])

    def test_snapshot_trims_to_the_keep_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "history.json"))
            for offset in range(5):
                pc.snapshot_ad_refs({}, today=DAY + timedelta(days=offset),
                                    path=path, keep=3)
            with open(path, encoding="utf-8") as f:
                history = json.load(f)
            self.assertEqual(len(history["days"]), 3)

    def test_previous_scores_come_from_the_newest_earlier_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "history.json"))
            code = pb.ref_code("BH", self.JID)
            pc.snapshot_ad_refs(
                {code: {"uniqueSenders": 1, "lastSeen": (DAY - timedelta(days=1)).isoformat()}},
                today=DAY - timedelta(days=1), path=path)
            scores = pc.previous_scores([self.JID], DAY, path=path)
            self.assertEqual(scores, {self.JID: 1.0})

    def test_previous_scores_is_none_without_an_earlier_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(pc.previous_scores(
                [self.JID], DAY, path=str(Path(tmp, "nope.json"))))

    def test_plan_does_not_write_a_snapshot_unless_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "history.json")
            saved = pc.HISTORY_PATH
            pc.HISTORY_PATH = str(path)
            try:
                pc.plan(membership({"1@g.us": {"promos": ["a"]}}), ["1@g.us"],
                        day=DAY, refs={}, record=False)
            finally:
                pc.HISTORY_PATH = saved
            self.assertFalse(path.exists())


class EngineWiringTests(unittest.TestCase):
    """promo_broadcast must stay inert until membership.json turns caps on, and
    must never let a caps problem stop a day's ads."""

    def setUp(self):
        # Hermetic: these fixtures use real campaign names (cv, genaie), so the
        # repo's cron manifest -- which may pause one of them -- must not leak in.
        saved = pc.MANIFEST_CANDIDATES
        pc.MANIFEST_CANDIDATES = ()
        self.addCleanup(setattr, pc, "MANIFEST_CANDIDATES", saved)

    def test_promo_key_strips_the_directory_suffix(self):
        self.assertEqual(pb.promo_key("cv-promo"), "cv")
        self.assertEqual(pb.promo_key("genaie-leads-promo"), "genaie-leads")
        self.assertEqual(pb.promo_key("cv"), "cv")

    def test_caps_none_disables_filtering(self):
        kept, decisions = pb.apply_caps("cv-promo", ["1@g.us"], DAY, caps=None)
        self.assertEqual(kept, ["1@g.us"])
        self.assertEqual(decisions, {})

    def test_missing_membership_file_leaves_every_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            kept, decisions = pb.apply_caps(
                "cv-promo", ["1@g.us"], DAY,
                membership_path=str(Path(tmp, "nope.json")))
            self.assertEqual(kept, ["1@g.us"])
            self.assertEqual(decisions, {})

    def test_caps_disabled_in_membership_leaves_every_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "membership.json")
            path.write_text(json.dumps(membership(
                {"1@g.us": {"promos": ["cv"], "activity": "quiet"}},
                caps_enabled=False, promos=("cv",))), encoding="utf-8")
            kept, decisions = pb.apply_caps("cv-promo", ["1@g.us"], DAY,
                                            membership_path=str(path))
            self.assertEqual(kept, ["1@g.us"])
            self.assertEqual(decisions, {})

    def test_caps_enabled_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "membership.json")
            path.write_text(json.dumps(membership(
                {"1@g.us": {"promos": ["cv", "genaie"], "daily_cap": 1}},
                caps_enabled=True, promos=("cv", "genaie"))), encoding="utf-8")
            allowed = []
            with redirect_stderr(io.StringIO()):
                for promo in ("cv-promo", "genaie-promo"):
                    kept, _ = pb.apply_caps(promo, ["1@g.us"], DAY,
                                            membership_path=str(path), record=False)
                    allowed.extend(kept and [promo] or [])
            self.assertEqual(len(allowed), 1,
                             "a cap of 1 must let exactly one campaign through")

    def test_a_broken_caps_module_fails_open(self):
        class Boom:
            @staticmethod
            def filter_for_promo(*a, **k):
                raise RuntimeError("nope")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "membership.json")
            path.write_text(json.dumps(membership(
                {"1@g.us": {"promos": ["cv"]}}, promos=("cv",))), encoding="utf-8")
            err = io.StringIO()
            with redirect_stderr(err):
                kept, decisions = pb.apply_caps("cv-promo", ["1@g.us"], DAY,
                                                caps=Boom,
                                                membership_path=str(path))
            self.assertEqual(kept, ["1@g.us"])
            self.assertIn("WARN", err.getvalue())


if __name__ == "__main__":
    unittest.main()
