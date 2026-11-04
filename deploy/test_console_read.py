"""Cover the console read bridge: the shape of each payload, its refusals, and
its promise never to write anything.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes them.

These run against the repository's real membership and campaigns, but assert
structure rather than totals. Frozen counts here would mean every added group
had to be reflected in a test, which is exactly the per-group busywork the
console exists to remove.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "workspace/bin/console-read"
LIB_DIR = ROOT / "workspace/lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import promo_membership  # noqa: E402

DAY = "2026-11-04"

GROUP_KEYS = {
    "jid", "name", "source", "status", "activity", "account", "dailyCapOverride",
    "promos", "weekly", "note", "cap", "replyScore", "allowedToday",
}
PREVIEW_KEYS = {
    "jid", "name", "account", "contactNumber", "ref", "variant", "caption",
    "cap", "sendsToday", "allowedToday", "weekly",
}
CAMPAIGN_KEYS = {
    "slug", "name", "cron", "hour", "minute", "timezone", "installer", "script",
    "failureAlertTo", "legacyNames", "timeoutSeconds", "captions", "tracking",
    "images", "targetCount", "weeklyCount", "accounts", "source", "error",
}


class ConsoleReadTests(unittest.TestCase):
    def run_bridge(self, *args, **environment):
        """Run the bridge in isolated mode, exactly as the console API does."""
        env = dict(os.environ)
        # Production sets this, and it keeps the test from leaving bytecode in
        # the tree, which the staging checks forbid.
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.update({key: str(value) for key, value in environment.items()})
        done = subprocess.run(
            [sys.executable, "-I", str(BRIDGE)] + [str(a) for a in args],
            capture_output=True, text=True, timeout=120, env=env,
        )
        return done

    def ok(self, *args):
        done = self.run_bridge(*args)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, "")
        return json.loads(done.stdout)

    def refused(self, *args, **environment):
        done = self.run_bridge(*args, **environment)
        self.assertNotEqual(done.returncode, 0)
        payload = json.loads(done.stdout)
        self.assertEqual(set(payload), {"error"}, payload)
        return done.returncode, payload["error"]

    # -- payload shapes ---------------------------------------------------

    def test_groups_describes_every_known_group_the_same_way(self):
        payload = self.ok("groups", "--date", DAY)
        self.assertEqual(set(payload), {"date", "groups"})
        self.assertEqual(payload["date"], DAY)
        self.assertTrue(payload["groups"])
        for row in payload["groups"]:
            self.assertEqual(set(row), GROUP_KEYS, row.get("jid"))
            self.assertTrue(row["jid"])
            self.assertIn(row["status"], promo_membership.STATUSES)
            self.assertIsInstance(row["promos"], list)
            self.assertIsInstance(row["allowedToday"], list)
            self.assertIsInstance(row["weekly"], dict)
            # Today's allowance can only go to campaigns that target the group.
            self.assertLessEqual(set(row["allowedToday"]), set(row["promos"]))

    def test_a_group_that_cannot_be_posted_to_is_never_targeted(self):
        """A non-active group keeps its configured campaigns in membership.json,
        so the console can show what reactivating it would restore. The
        invariant that matters is that no campaign actually targets it."""
        m = promo_membership.load(
            promo_membership.default_path(str(ROOT / "workspace")))
        inactive = {jid for jid, entry in promo_membership.groups(m).items()
                    if promo_membership.status_of(entry) != "active"}
        self.assertTrue(inactive, "expected the fixture to have inactive groups")
        for promo in promo_membership.promo_names(m):
            daily, weekly = promo_membership.groups_for_promo(m, promo)
            self.assertEqual(inactive & (set(daily) | set(weekly)), set(), promo)

    def test_campaigns_describes_every_declared_promo_in_slot_order(self):
        payload = self.ok("campaigns", "--date", DAY)
        self.assertEqual(set(payload), {"date", "campaigns"})
        records = payload["campaigns"]
        self.assertTrue(records)
        for record in records:
            self.assertEqual(set(record), CAMPAIGN_KEYS, record.get("slug"))
            self.assertTrue(record["slug"])
            self.assertIsNone(record["error"], record["slug"])
            self.assertTrue(record["captions"], record["slug"])
            self.assertTrue(record["script"].endswith(".py"))
        slots = [(r["hour"], r["minute"]) for r in records]
        self.assertEqual(slots, sorted(slots))

    def test_preview_renders_the_caption_the_cron_would_send(self):
        payload = self.ok("preview", "borehole-drilling", "--date", DAY)
        self.assertEqual(set(payload), {"slug", "captions", "tracking", "groups"})
        self.assertTrue(payload["groups"])
        for row in payload["groups"]:
            self.assertEqual(set(row), PREVIEW_KEYS, row.get("jid"))
            self.assertTrue(row["caption"])
            # A tracked campaign stamps its ref into the text it sends, so the
            # preview is only trustworthy if the ref is actually in there.
            self.assertTrue(row["ref"])
            self.assertIn(row["ref"], row["caption"])
            self.assertNotIn("<REF>", row["caption"])
            self.assertNotIn("<ref_link>", row["caption"])
            self.assertNotIn("<bot_number>", row["caption"])
            # The engine's variant index is 0-based; the console adds one only
            # when it shows it, exactly as post_ad.py --dry-run does.
            self.assertIn(row["variant"], range(len(payload["captions"])))

    def test_every_declared_campaign_can_be_previewed(self):
        for record in self.ok("campaigns", "--date", DAY)["campaigns"]:
            payload = self.ok("preview", record["slug"], "--date", DAY)
            self.assertEqual(payload["slug"], record["slug"])
            self.assertEqual(len(payload["groups"]), record["targetCount"])

    def test_overview_agrees_with_the_routes_it_summarizes(self):
        overview = self.ok("overview", "--date", DAY)
        groups = self.ok("groups", "--date", DAY)["groups"]
        campaigns = self.ok("campaigns", "--date", DAY)["campaigns"]
        self.assertEqual(overview["groupTotal"], len(groups))
        self.assertEqual(overview["campaignTotal"], len(campaigns))
        self.assertEqual(sum(overview["groupsByStatus"].values()), len(groups))
        self.assertEqual(
            overview["plannedSendsToday"],
            sum(len(r["allowedToday"]) for r in groups if r["status"] == "active"))
        for account in overview["accounts"]:
            self.assertEqual(set(account), {"id", "number", "isContact"})

    def test_the_same_day_always_gives_the_same_answer(self):
        self.assertEqual(self.run_bridge("groups", "--date", DAY).stdout,
                         self.run_bridge("groups", "--date", DAY).stdout)

    # -- refusals ---------------------------------------------------------

    def test_an_absent_campaign_is_reported_as_absent(self):
        code, message = self.refused("preview", "no-such-campaign")
        self.assertEqual(code, 3)
        self.assertIn("no-such-campaign", message)

    def test_bad_arguments_are_refused_as_messages_not_tracebacks(self):
        for args in ((), ("frobnicate",), ("groups", "--verbose"), ("preview",),
                     ("groups", "--date"), ("groups", "--date", "2026-13-99"),
                     ("groups", "extra", "more")):
            code, _ = self.refused(*args)
            self.assertEqual(code, 1, args)

    def test_a_missing_cron_manifest_is_reported_clearly(self):
        with tempfile.TemporaryDirectory() as empty:
            code, message = self.refused(
                "campaigns", GENAIE_PROMO_CRONS=os.path.join(empty, "absent.json"))
            self.assertEqual(code, 1)
            self.assertIn("cron manifest", message)

    def test_an_explicit_cron_manifest_is_preferred(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "promo-crons.json"
            manifest.write_text(json.dumps({"timeoutSeconds": 11, "promos": []}))
            payload = json.loads(self.run_bridge(
                "campaigns", GENAIE_PROMO_CRONS=manifest).stdout)
            self.assertEqual(payload["campaigns"], [])

    # -- the read-only promise -------------------------------------------

    def snapshot(self):
        """Size and mtime of everything the bridge reads."""
        seen = {}
        for base in (ROOT / "workspace/cron", ROOT / "workspace/lib",
                     ROOT / "deploy/promo-crons.json"):
            for path in ([base] if base.is_file() else sorted(base.rglob("*"))):
                if path.is_file():
                    stat = path.stat()
                    seen[str(path)] = (stat.st_size, stat.st_mtime_ns)
        return seen

    def test_reading_changes_nothing_in_the_tree(self):
        before = self.snapshot()
        for args in (("overview",), ("groups",), ("campaigns",),
                     ("preview", "borehole-drilling")):
            self.assertEqual(self.run_bridge(*args, "--date", DAY).returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_reading_never_records_a_cap_snapshot(self):
        """promo_caps.plan writes the ad-ref history when asked to record. The
        bridge must never ask: a read must not move the rotation on."""
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "ad-ref-history.json"
            refs = Path(directory) / "ad-refs.json"
            for args in (("groups",), ("overview",), ("preview", "borehole-drilling")):
                done = self.run_bridge(
                    *args, "--date", DAY,
                    PROMO_CAPS_HISTORY=history, PROMO_AD_REFS_PATH=refs)
                self.assertEqual(done.returncode, 0, done.stderr)
            self.assertFalse(history.exists(), "the bridge recorded a cap snapshot")
            self.assertFalse(refs.exists())
            self.assertEqual(sorted(os.listdir(directory)), [])


if __name__ == "__main__":
    unittest.main()
