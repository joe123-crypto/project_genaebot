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
    "promos", "pausedPromos", "weekly", "note", "cap", "replyScore", "allowedToday", "metrics",
}
PREVIEW_KEYS = {
    "jid", "name", "account", "contactNumber", "ref", "variant", "caption",
    "cap", "paused", "sendsToday", "allowedToday", "weekly",
}
CAMPAIGN_KEYS = {
    "slug", "name", "cron", "hour", "minute", "timezone", "installer", "script",
    "failureAlertTo", "legacyNames", "timeoutSeconds", "captions", "tracking",
    "images", "assets", "targetCount", "weeklyCount", "accounts", "source",
    "error", "enabled", "storeEnabled", "cronEnabled", "scheduleMinutes", "scheduleEdit",
}
AD_ASSET_KEYS = {"mode", "warning", "lastCheck", "source", "fetchedAt", "note"}
ASSET_KEYS = {"filename", "url", "sha256", "bytes", "contentType", "rank"}


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

    def ok_with(self, args, **environment):
        """``ok``, but with the bridge's environment changed -- for the paths
        that depend on what is configured rather than on what is declared."""
        done = self.run_bridge(*args, **environment)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def refused(self, *args, **environment):
        done = self.run_bridge(*args, **environment)
        self.assertNotEqual(done.returncode, 0)
        payload = json.loads(done.stdout)
        self.assertEqual(set(payload), {"error"}, payload)
        return done.returncode, payload["error"]

    # -- payload shapes ---------------------------------------------------

    def test_group_controls_match_overview_without_loading_campaign_assets(self):
        groups = self.ok("groups", "--date", DAY)
        overview = self.ok("overview", "--date", DAY)
        context = groups["context"]
        for key in ("writes", "actions", "accounts", "capSettings"):
            self.assertEqual(context[key], overview[key])
        self.assertEqual({row["slug"]: row for row in context["campaigns"]}, {
            row["slug"]: {key: row[key] for key in ("slug", "name", "enabled")}
            for row in overview["campaigns"]})

    def test_groups_describes_every_known_group_the_same_way(self):
        payload = self.ok("groups", "--date", DAY)
        self.assertEqual(set(payload), {"date", "disabledPromos", "groups", "discoveredGroups", "groupDiscovery", "context"})
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
        self.assertEqual(set(payload), {"date", "adAssets", "campaigns", "deletions"})
        self.assertEqual(set(payload["adAssets"]), AD_ASSET_KEYS)
        records = payload["campaigns"]
        self.assertTrue(records)
        for record in records:
            self.assertEqual(set(record), CAMPAIGN_KEYS, record.get("slug"))
            self.assertTrue(record["slug"])
            self.assertIsNone(record["error"], record["slug"])
            self.assertTrue(record["captions"], record["slug"])
            self.assertTrue(record["script"].endswith(".py"))
            self.assertIsInstance(record["assets"], list)
            for asset in record["assets"]:
                self.assertEqual(set(asset), ASSET_KEYS, record["slug"])
        slots = [(r["hour"], r["minute"]) for r in records]
        self.assertEqual(slots, sorted(slots))

    def test_no_index_and_no_mirror_costs_the_pictures_and_nothing_else(self):
        """The whole point of the bridge's catalogue being failure-tolerant. With
        no credentials, no snapshot and no mirror there is nothing to show, and
        the campaigns screen must still load in full and say why its tiles are
        empty rather than implying the campaigns have no pictures."""
        with tempfile.TemporaryDirectory() as empty:
            payload = self.ok_with(("campaigns", "--date", DAY),
                                   GENAIE_FIRESTORE_DIR=empty,
                                   GENAIE_AD_ASSETS_MIRROR=os.path.join(empty, "absent.json"),
                                   GENAIE_AD_ASSETS_BASE_URL="")
        self.assertTrue(payload["campaigns"])
        for record in payload["campaigns"]:
            self.assertEqual(set(record), CAMPAIGN_KEYS, record.get("slug"))
            self.assertEqual(record["assets"], [])
            self.assertTrue(record["captions"], record["slug"])
        self.assertIsNone(payload["adAssets"]["source"])
        self.assertTrue(payload["adAssets"]["note"])

    def test_the_committed_mirror_carries_the_pictures_when_firestore_cannot(self):
        """What production actually hit: the Firestore quota was exhausted and,
        in repo mode, no run had ever written a snapshot. Every tile went blank
        over pictures that were sitting in the bucket."""
        base = "https://ads.example.test"
        with tempfile.TemporaryDirectory() as empty:
            payload = self.ok_with(("campaigns", "--date", DAY),
                                   GENAIE_FIRESTORE_DIR=empty,
                                   GENAIE_AD_ASSETS_BASE_URL=base)
        self.assertEqual(payload["adAssets"]["source"], "mirror")
        shown = [r for r in payload["campaigns"] if r["assets"]]
        self.assertTrue(shown, "the committed mirror should name some pictures")
        for record in shown:
            for asset in record["assets"]:
                self.assertEqual(set(asset), ASSET_KEYS, record["slug"])
                self.assertTrue(asset["url"].startswith(base + "/ads/"))
            # The mirror is the index for the same campaign the post script
            # declares, so the two lists must still be about the same pictures.
            self.assertLessEqual({a["filename"] for a in record["assets"]},
                                 set(record["images"]), record["slug"])

    def test_preview_renders_the_caption_the_cron_would_send(self):
        payload = self.ok("preview", "borehole-drilling", "--date", DAY)
        self.assertEqual(set(payload),
                         {"slug", "enabled", "captions", "tracking", "groups"})
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

    # -- switched-off campaigns -------------------------------------------

    def declared_disabled(self):
        """Slugs the repository's own manifest switches off. Derived, not
        hardcoded: re-enabling a campaign is a deliberate config change and
        should not have to be mirrored here."""
        manifest = json.loads((ROOT / "deploy/promo-crons.json").read_text())
        return {promo["slug"] for promo in manifest["promos"]
                if promo.get("enabled", True) is False}

    def test_overview_reports_shadow_mode_off_by_default(self):
        with tempfile.TemporaryDirectory() as state:
            done = self.run_bridge("overview", "--date", DAY, GENAIE_CONFIG_SOURCE="",
                                   GENAIE_FIRESTORE_DIR=state)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["configShadow"],
                         {"mode": "file", "warning": None, "lastCheck": None})

    def test_overview_carries_the_newest_shadow_check(self):
        with tempfile.TemporaryDirectory() as state:
            check = {"checkedAt": "2026-11-05T06:00:00Z", "status": "drift",
                     "source": "firestore", "degraded": None,
                     "problems": ["groups/x@g.us differs in status"], "notes": []}
            with open(os.path.join(state, "shadow.json"), "w") as handle:
                json.dump(check, handle)
            done = self.run_bridge("overview", "--date", DAY, GENAIE_CONFIG_SOURCE="shadow",
                                   GENAIE_FIRESTORE_DIR=state)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, "")
        shadow = json.loads(done.stdout)["configShadow"]
        self.assertEqual((shadow["mode"], shadow["lastCheck"]), ("shadow", check))

    def test_every_campaign_says_whether_it_is_switched_on(self):
        disabled = self.declared_disabled()
        self.assertTrue(disabled, "expected the manifest to switch one promo off")
        records = self.ok("campaigns", "--date", DAY)["campaigns"]
        for record in records:
            self.assertIsInstance(record["enabled"], bool, record["slug"])
            self.assertEqual(record["enabled"], record["slug"] not in disabled,
                             record["slug"])
        self.assertEqual(self.ok("groups", "--date", DAY)["disabledPromos"],
                         sorted(disabled))

    def test_a_switched_off_campaign_is_never_predicted_to_send(self):
        """It installs no cron, so it cannot post today however the caps fall.
        It stays in each group's configured promos, because the console has to
        be able to show what switching it back on would restore."""
        disabled = self.declared_disabled()
        self.assertTrue(disabled, "expected the manifest to switch one promo off")
        payload = self.ok("groups", "--date", DAY)
        configured = set()
        for row in payload["groups"]:
            configured |= set(row["promos"]) & disabled
            self.assertEqual(set(row["allowedToday"]) & disabled, set(), row["jid"])
        self.assertTrue(configured,
                        "expected a switched-off promo to still be configured")

    def test_previewing_a_switched_off_campaign_shows_copy_but_no_sends(self):
        for slug in sorted(self.declared_disabled()):
            payload = self.ok("preview", slug, "--date", DAY)
            self.assertFalse(payload["enabled"], slug)
            self.assertTrue(payload["captions"], slug)
            self.assertTrue(payload["groups"], slug)
            # The copy is worth reading; the schedule is not. Nothing may claim
            # this campaign posts today.
            for row in payload["groups"]:
                self.assertFalse(row["sendsToday"], row["jid"])

    def test_the_same_day_always_gives_the_same_answer(self):
        # Campaign decisions are deterministic; rolling telemetry is live even
        # when the operator asks to inspect a historical campaign date.
        first = self.ok("groups", "--date", DAY)
        second = self.ok("groups", "--date", DAY)
        for payload in (first, second):
            for row in payload["groups"] + payload["discoveredGroups"]:
                row.pop("metrics", None)
        self.assertEqual(first, second)

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

    def mutated_manifest(self, directory, mutate):
        """The repository's own manifest with one promo altered, so these keep
        real script paths and stay about the enabled key alone."""
        manifest = json.loads((ROOT / "deploy/promo-crons.json").read_text())
        mutate(manifest["promos"][0])
        path = Path(directory) / "promo-crons.json"
        path.write_text(json.dumps(manifest))
        return path

    def test_an_absent_enabled_key_means_switched_on(self):
        """install-promo.sh, railway-release.py and report-release-ready.py all
        default it to true, so a promo that never mentions it must read as on."""
        with tempfile.TemporaryDirectory() as directory:
            path = self.mutated_manifest(
                directory, lambda promo: promo.pop("enabled", None))
            payload = json.loads(self.run_bridge(
                "campaigns", GENAIE_PROMO_CRONS=path).stdout)
            self.assertTrue(payload["campaigns"][0]["enabled"])

    def test_a_non_boolean_enabled_is_refused_not_guessed(self):
        """A string is truthy in Python, so guessing would silently promise
        sends for a campaign the operator wrote off. railway-release.py rejects
        a non-bool here too."""
        with tempfile.TemporaryDirectory() as directory:
            path = self.mutated_manifest(
                directory, lambda promo: promo.update({"enabled": "false"}))
            code, message = self.refused("campaigns", GENAIE_PROMO_CRONS=path)
            self.assertEqual(code, 1)
            self.assertIn("enabled", message)

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

    # -- what a console may edit -----------------------------------------

    def overview_writes(self, **environment):
        done = self.run_bridge("overview", "--date", DAY, **environment)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)["writes"]

    def test_writes_are_reported_off_without_the_write_key(self):
        # Blank is how the gateway looks before a key is installed.
        self.assertIs(self.overview_writes(**{WRITE_KEY: ""})["enabled"], False)
        self.assertIs(self.overview_writes(**{WRITE_KEY: "   "})["enabled"], False)

    def test_the_editable_fields_are_the_writers_own(self):
        # The console gates every control on this, so it must be exactly what the
        # writer will accept -- not a copy that can drift from it.
        import operator_writer
        writes = self.ok("overview", "--date", DAY)["writes"]
        self.assertEqual(writes["fields"], dict((c, list(f)) for c, f
                                                in operator_writer.EDITABLE.items()))
        self.assertEqual(writes["delete"], ["groups"])
        self.assertIn("paused_promos", writes["fields"]["groups"])

    def test_group_actions_are_reported_apart_from_the_write_key(self):
        # Leaving a WhatsApp group and forgetting a group's local records write
        # no Firestore document, so they must not disappear when the write key
        # is absent -- a console would then hide a working control.
        payload = self.ok("overview", "--date", DAY)
        self.assertEqual(set(payload["actions"]), {"leaveGroup", "clearRecords"})
        without = json.loads(
            self.run_bridge("overview", "--date", DAY, **{WRITE_KEY: ""}).stdout)
        self.assertIs(without["writes"]["enabled"], False)
        self.assertIs(without["actions"]["leaveGroup"], True)

    def test_the_group_actions_switch_is_reported(self):
        # GENAIE_GROUP_ACTIONS=0 is the gateway's way of turning the surface
        # off; the console shows the controls disabled rather than failing.
        done = self.run_bridge("overview", "--date", DAY, GENAIE_GROUP_ACTIONS="0")
        self.assertEqual(done.returncode, 0, done.stderr)
        actions = json.loads(done.stdout)["actions"]
        self.assertIs(actions["leaveGroup"], False)
        self.assertIs(actions["clearRecords"], False)

    def test_the_write_key_is_reported_by_presence_never_by_value(self):
        # A canary rather than anything key-shaped: the bridge checks presence
        # only, so the value never needs to decode, just to be findable.
        secret = "WRITE-KEY-CANARY-must-never-be-printed"
        done = self.run_bridge("overview", "--date", DAY, **{WRITE_KEY: secret})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIs(json.loads(done.stdout)["writes"]["enabled"], True)
        self.assertNotIn(secret, done.stdout + done.stderr)

    def test_writes_say_whether_a_saved_change_reaches_the_campaigns(self):
        # Before the cutover a write lands in Firestore while runs still act on
        # membership.json; a console has to be able to say so.
        for mode in ("file", "shadow"):
            with self.subTest(mode=mode):
                writes = self.overview_writes(GENAIE_CONFIG_SOURCE=mode)
                self.assertIs(writes["affectsProduction"], False)

    def test_after_the_cutover_a_saved_change_reaches_the_campaigns(self):
        # Checked in-process: a firestore-mode overview would need a live
        # Firestore, but this answer depends only on the mode.
        from unittest import mock
        console = load_console_read()
        with mock.patch.dict(os.environ, {"GENAIE_CONFIG_SOURCE": "firestore"}):
            self.assertIs(console.write_capabilities()["affectsProduction"], True)

    def test_cap_settings_come_in_the_shape_a_form_edits(self):
        m = promo_membership.load(promo_membership.default_path(str(ROOT / "workspace")))
        settings = self.ok("overview", "--date", DAY)["capSettings"]
        self.assertEqual(set(settings), {"enabled", "defaultActivity", "bands"})
        self.assertIs(settings["enabled"], promo_membership.caps_enabled(m))
        self.assertEqual(settings["bands"], dict(
            (tier, [low, high]) for tier, (low, high) in promo_membership.bands(m).items()))

    def test_a_campaign_reports_both_switches(self):
        # cv has no cron registered (manifest enabled: false), so it cannot post
        # whatever its store switch says; the console must not offer a toggle as
        # if it could.
        campaigns = dict((row["slug"], row) for row
                         in self.ok("campaigns", "--date", DAY)["campaigns"])
        self.assertIs(campaigns["cv"]["cronEnabled"], False)
        self.assertIs(campaigns["cv"]["enabled"], False)
        self.assertIs(campaigns["genaie"]["cronEnabled"], True)
        self.assertIs(campaigns["genaie"]["storeEnabled"], True)
        for row in campaigns.values():
            self.assertIs(row["enabled"], row["storeEnabled"] and row["cronEnabled"],
                          row["slug"])


WRITE_KEY = "GCP_SERVICE_ACCOUNT_WRITE_B64"


def load_console_read():
    """console-read has no extension, so it loads by path -- the same way the
    review regression tests reach its internals."""
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("console_read_inproc", str(BRIDGE))
    spec = importlib.util.spec_from_loader("console_read_inproc", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class AssetEntryTests(unittest.TestCase):
    """The join itself, offline. The subprocess tests above run without
    credentials, so they prove the empty case; this proves the full one without
    asking CI for a network."""

    def setUp(self):
        self.console = load_console_read()
        self.console._CATALOGUE["value"] = {
            "source": "firestore", "fetchedAt": "2026-11-09T02:53:12Z", "note": None,
            "campaigns": {"borehole-drilling": [
                {"id": "borehole-drilling-1", "campaign": "borehole-drilling",
                 "filename": "borehole-1.jpg", "key": "ads/borehole-drilling/aa.jpg",
                 "url": "https://ads.example.test/ads/borehole-drilling/aa.jpg",
                 "sha256": "a" * 64, "bytes": 243016, "content_type": "image/jpeg",
                 "rank": 10, "enabled": True, "comment": None},
                {"id": "borehole-drilling-2", "campaign": "borehole-drilling",
                 "filename": "borehole-2.jpg", "key": "ads/borehole-drilling/bb.jpg",
                 "url": "https://ads.example.test/ads/borehole-drilling/bb.jpg",
                 "sha256": "b" * 64, "bytes": 11918, "content_type": "image/jpeg",
                 "rank": 20, "enabled": True, "comment": None}]}}

    def test_a_campaigns_pictures_carry_the_address_of_their_bytes(self):
        entries = self.console.asset_entries("borehole-drilling")
        self.assertEqual([e["filename"] for e in entries],
                         ["borehole-1.jpg", "borehole-2.jpg"])
        self.assertEqual(entries[0]["url"],
                         "https://ads.example.test/ads/borehole-drilling/aa.jpg")
        self.assertEqual(entries[0]["contentType"], "image/jpeg")
        self.assertEqual(entries[0]["rank"], 10)

    def test_the_index_order_is_kept_because_it_is_the_rotation_order(self):
        entries = self.console.asset_entries("borehole-drilling")
        self.assertEqual([e["rank"] for e in entries], [10, 20])

    def test_a_campaign_the_index_does_not_mention_has_no_pictures_not_an_error(self):
        self.assertEqual(self.console.asset_entries("fruit-trees"), [])

    def test_the_key_never_reaches_the_console_because_the_url_already_has_it(self):
        for entry in self.console.asset_entries("borehole-drilling"):
            self.assertNotIn("key", entry)
            self.assertNotIn("enabled", entry)


class PausedAssignmentTests(unittest.TestCase):
    """A paused assignment has to stay visible: the campaign page's list of where
    it goes is the only place it can be resumed from."""

    def setUp(self):
        import copy
        from datetime import date
        self.console = load_console_read()
        self.day = date(2026, 11, 4)
        base = promo_membership.load(self.console.MEMBERSHIP)
        self.m = copy.deepcopy(base)
        self.jid = next(jid for jid, entry in self.m["groups"].items()
                        if promo_membership.is_active(entry)
                        and "genaie" in (entry.get("promos") or []))
        self.m["groups"][self.jid]["paused_promos"] = ["genaie"]
        self.console._RESOLVED["config"] = {
            "mode": "file", "source": "file", "membership": self.m, "doc": {},
            "switches": {}, "storeSwitches": {}, "cronSwitches": {},
            "report": {"quarantined": {}, "warnings": [], "campaigns": {}}, "notes": []}

    def test_a_paused_group_stays_in_the_campaigns_list_marked_paused(self):
        rows = dict((row["jid"], row) for row
                    in self.console.preview("genaie", self.day)["groups"])
        self.assertIn(self.jid, rows)
        self.assertIs(rows[self.jid]["paused"], True)
        self.assertIs(rows[self.jid]["sendsToday"], False)
        self.assertTrue(any(not row["paused"] for row in rows.values()))

    def test_the_group_row_lists_the_pause_and_plans_no_send_for_it(self):
        row = next(row for row in self.console.group_rows(self.day)
                   if row["jid"] == self.jid)
        self.assertEqual(row["pausedPromos"], ["genaie"])
        self.assertIn("genaie", row["promos"])
        self.assertNotIn("genaie", row["allowedToday"])


if __name__ == "__main__":
    unittest.main()
