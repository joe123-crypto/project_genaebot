"""Cover the console read bridge: the shape of each payload, its refusals, and
its promise never to write anything.

These used to run against the repository's own roster and cron manifest. There
is no committed config left, so the bridge is run against a synthetic one --
``deploy/promo_fixture`` written out as a last-known-good snapshot, which is
what ``operator_config`` serves when Firestore cannot be reached. That is closer
to production than the old arrangement, not further from it: the bridge now
takes the same path here as it does on the gateway, and a test no longer starts
failing because somebody added a group in the console.

Structure is still what is asserted rather than totals, with one change: the
fixture is fixed, so a count that says something (a campaign with no cron, a
paused assignment) can be named outright.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes them.
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
for _path in (str(ROOT / "workspace/lib"), str(ROOT / "deploy")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import promo_fixture  # noqa: E402
import promo_membership  # noqa: E402

# The fixture's Friday, so its weekly campaign has an occurrence today.
DAY = promo_fixture.DAY.isoformat()
BASE_URL = "https://assets.example.test"
WRITE_KEY = "GCP_SERVICE_ACCOUNT_WRITE_B64"

GROUP_KEYS = {
    "jid", "name", "source", "status", "activity", "account", "dailyCapOverride",
    "promos", "pausedPromos", "weekly", "note", "cap", "replyScore", "allowedToday", "metrics",
}
PREVIEW_KEYS = {
    "jid", "name", "account", "contactNumber", "ref", "variant", "caption",
    "cap", "paused", "sendsToday", "allowedToday", "weekly",
}
CAMPAIGN_KEYS = {
    "slug", "name", "cron", "hour", "minute", "timezone",
    "failureAlertTo", "timeoutSeconds", "captions", "tracking",
    "images", "assets", "targetCount", "weeklyCount", "accounts", "source",
    "error", "enabled", "storeEnabled", "cronEnabled", "scheduleMinutes", "scheduleEdit",
    "jobName", "scheduleStatus",
}
AD_ASSET_KEYS = {"mode", "warning", "lastCheck", "source", "fetchedAt", "note"}
ASSET_KEYS = {"filename", "url", "sha256", "bytes", "contentType", "rank"}


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


class BridgeCase(unittest.TestCase):
    """One offline config on disk, and the bridge run against it as the console
    API runs it: a fresh isolated interpreter, per call."""

    CONFIG = None          # a subclass may hand the snapshot a different config
    ENABLED = None
    ASSETS = True

    def setUp(self):
        self.state = tempfile.TemporaryDirectory()
        self.addCleanup(self.state.cleanup)
        self.firestore = os.path.join(self.state.name, "firestore")
        config = self.CONFIG or promo_fixture.membership()
        promo_fixture.snapshot(self.firestore, config, self.ENABLED)
        if self.ASSETS:
            promo_fixture.asset_index(self.firestore, config)
        self.config = config

    def bridge_env(self, **overrides):
        env = dict(os.environ)
        # Production sets this, and it keeps the test from leaving bytecode in
        # the tree, which the staging checks forbid.
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.update({"OPENCLAW_STATE_DIR": self.state.name,
                    "GENAIE_FIRESTORE_DIR": self.firestore,
                    "GENAIE_CONFIG_SOURCE": "firestore",
                    "GENAIE_AD_ASSETS_BASE_URL": BASE_URL})
        env.update({key: str(value) for key, value in overrides.items()})
        return env

    def run_bridge(self, *args, **environment):
        return subprocess.run(
            [sys.executable, "-I", str(BRIDGE)] + [str(a) for a in args],
            capture_output=True, text=True, timeout=120,
            env=self.bridge_env(**environment))

    def ok(self, *args, **environment):
        done = self.run_bridge(*args, **environment)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, "")
        return json.loads(done.stdout)

    def ok_with(self, args, **environment):
        """``ok``, but with the bridge's environment changed -- for the paths
        that depend on what is configured."""
        done = self.run_bridge(*args, **environment)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def refused(self, *args, **environment):
        done = self.run_bridge(*args, **environment)
        self.assertNotEqual(done.returncode, 0)
        payload = json.loads(done.stdout)
        self.assertEqual(set(payload), {"error"}, payload)
        return done.returncode, payload["error"]


class ConsoleReadTests(BridgeCase):

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
        self.assertEqual(len(payload["groups"]), len(self.config["groups"]))
        for row in payload["groups"]:
            self.assertEqual(set(row), GROUP_KEYS, row.get("jid"))
            self.assertTrue(row["jid"])
            self.assertIn(row["status"], promo_membership.STATUSES)
            self.assertIsInstance(row["promos"], list)
            self.assertIsInstance(row["allowedToday"], list)
            self.assertIsInstance(row["weekly"], dict)
            # Today's allowance can only go to campaigns that target the group.
            self.assertLessEqual(set(row["allowedToday"]),
                                 set(row["promos"]) | set(row["weekly"]))

    def test_a_group_that_cannot_be_posted_to_is_never_targeted(self):
        """A non-active group keeps its configured campaigns, so the console can
        show what reactivating it would restore. The invariant that matters is
        that no campaign actually targets it."""
        inactive = {jid for jid, entry in self.config["groups"].items()
                    if promo_membership.status_of(entry) != "active"}
        self.assertTrue(inactive, "expected the fixture to have inactive groups")
        for row in self.ok("groups", "--date", DAY)["groups"]:
            if row["jid"] in inactive:
                self.assertEqual(row["allowedToday"], [], row["jid"])
        for record in self.ok("campaigns", "--date", DAY)["campaigns"]:
            previewed = {row["jid"] for row
                         in self.ok("preview", record["slug"], "--date", DAY)["groups"]}
            self.assertEqual(inactive & previewed, set(), record["slug"])

    def test_campaigns_describes_every_campaign_in_slot_order(self):
        payload = self.ok("campaigns", "--date", DAY)
        self.assertEqual(set(payload), {"date", "adAssets", "campaigns", "deletions"})
        self.assertEqual(set(payload["adAssets"]), AD_ASSET_KEYS)
        records = payload["campaigns"]
        self.assertEqual({r["slug"] for r in records}, set(self.config["promos"]))
        for record in records:
            self.assertEqual(set(record), CAMPAIGN_KEYS, record.get("slug"))
            self.assertTrue(record["slug"])
            self.assertIsNone(record["error"], record["slug"])
            self.assertTrue(record["captions"], record["slug"])
            self.assertEqual(record["source"], "console", record["slug"])
            self.assertIsInstance(record["assets"], list)
            for asset in record["assets"]:
                self.assertEqual(set(asset), ASSET_KEYS, record["slug"])
        slots = [(r["hour"], r["minute"]) for r in records]
        self.assertEqual(slots, sorted(slots))

    def test_a_converted_campaign_reports_the_job_name_it_kept(self):
        # alpha carries jobName; the other two never had a script, so theirs is
        # derived. The console shows the name so an operator can find the job.
        records = dict((r["slug"], r) for r
                       in self.ok("campaigns", "--date", DAY)["campaigns"])
        self.assertEqual(records["alpha"]["jobName"], "alpha-promo")
        self.assertEqual(records["beta"]["jobName"], "console-beta-promo")

    def test_the_pictures_carry_the_address_of_their_bytes(self):
        for record in self.ok("campaigns", "--date", DAY)["campaigns"]:
            self.assertEqual([a["filename"] for a in record["assets"]],
                             record["images"], record["slug"])
            for asset in record["assets"]:
                self.assertTrue(asset["url"].startswith(BASE_URL + "/ads/"),
                                asset["url"])

    def test_no_index_costs_the_pictures_and_nothing_else(self):
        """The whole point of the bridge's catalogue being failure-tolerant. The
        pictures live in R2 behind an index in Firestore; with neither readable
        there is nothing to show, and the campaigns screen must still load in
        full and say why its tiles are empty rather than implying the campaigns
        have no pictures."""
        with tempfile.TemporaryDirectory() as empty:
            promo_fixture.snapshot(empty, self.config, self.ENABLED)
            payload = self.ok_with(("campaigns", "--date", DAY),
                                   GENAIE_FIRESTORE_DIR=empty,
                                   GENAIE_AD_ASSETS_BASE_URL="")
        self.assertTrue(payload["campaigns"])
        for record in payload["campaigns"]:
            self.assertEqual(set(record), CAMPAIGN_KEYS, record.get("slug"))
            self.assertEqual(record["assets"], [])
            self.assertTrue(record["captions"], record["slug"])
        self.assertIsNone(payload["adAssets"]["source"])
        self.assertTrue(payload["adAssets"]["note"])

    def test_the_index_snapshot_carries_the_pictures_when_firestore_cannot(self):
        """What production actually hit: the Firestore quota was exhausted and
        every picture tile went blank over bytes that were sitting in the
        bucket. The index snapshot is what keeps them visible."""
        payload = self.ok("campaigns", "--date", DAY)
        self.assertEqual(payload["adAssets"]["source"], "snapshot")
        shown = [r for r in payload["campaigns"] if r["assets"]]
        self.assertTrue(shown, "the index snapshot should name some pictures")
        for record in shown:
            self.assertLessEqual({a["filename"] for a in record["assets"]},
                                 set(record["images"]), record["slug"])

    def test_preview_renders_the_caption_the_run_would_send(self):
        payload = self.ok("preview", "alpha", "--date", DAY)
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
            # when it shows it, exactly as a dry run does.
            self.assertIn(row["variant"], range(len(payload["captions"])))

    def test_every_campaign_can_be_previewed(self):
        for record in self.ok("campaigns", "--date", DAY)["campaigns"]:
            payload = self.ok("preview", record["slug"], "--date", DAY)
            self.assertEqual(payload["slug"], record["slug"])
            # A preview lists every assignment, paused ones included, so that a
            # paused group can be found and resumed; targetCount is what the
            # campaign would actually post to.
            paused = sum(record["slug"] in (entry.get("paused_promos") or [])
                         for entry in self.config["groups"].values())
            self.assertEqual(len(payload["groups"]),
                             record["targetCount"] + paused, record["slug"])
            self.assertEqual(sum(row["paused"] for row in payload["groups"]), paused)

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

    def disabled(self):
        """Campaigns the operator has switched off in the store."""
        return {slug for slug, doc in self.config["promos"].items()
                if not doc.get("enabled")}

    def test_every_campaign_says_whether_it_is_switched_on(self):
        off = self.disabled()
        self.assertTrue(off, "expected the fixture to switch one campaign off")
        for record in self.ok("campaigns", "--date", DAY)["campaigns"]:
            self.assertIsInstance(record["enabled"], bool, record["slug"])
            self.assertEqual(record["enabled"], record["slug"] not in off,
                             record["slug"])
        self.assertEqual(self.ok("groups", "--date", DAY)["disabledPromos"],
                         sorted(off))

    def test_a_switched_off_campaign_is_never_predicted_to_send(self):
        """It stands down whatever the caps say. It stays in each group's
        configured campaigns, because the console has to be able to show what
        switching it back on would restore."""
        off = self.disabled()
        payload = self.ok("groups", "--date", DAY)
        configured = set()
        for row in payload["groups"]:
            configured |= set(row["promos"]) & off
            self.assertEqual(set(row["allowedToday"]) & off, set(), row["jid"])
        self.assertTrue(configured,
                        "expected a switched-off campaign to still be configured")

    def test_previewing_a_switched_off_campaign_shows_copy_but_no_sends(self):
        for slug in sorted(self.disabled()):
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

    def test_no_config_at_all_is_reported_rather_than_guessed(self):
        """There is no second source. With neither Firestore nor a snapshot the
        bridge has nothing to describe and must say so, not show an empty
        console that reads as "you have no groups"."""
        with tempfile.TemporaryDirectory() as empty:
            code, message = self.refused("groups", "--date", DAY,
                                         GENAIE_FIRESTORE_DIR=empty)
        self.assertEqual(code, 1)
        self.assertIn("service account", message)

    def test_asking_for_a_retired_source_is_still_answered(self):
        """``GENAIE_CONFIG_SOURCE`` selected the committed files once. Setting it
        to one of those now has to be harmless -- the store is the only source --
        and the resolver's note says so rather than the read failing."""
        payload = self.ok_with(("overview", "--date", DAY),
                               GENAIE_CONFIG_SOURCE="file")
        self.assertTrue(payload["campaigns"])
        source = payload["configSource"]
        self.assertEqual(source["mode"], "firestore")
        self.assertTrue(any("no longer selects anything" in note
                            for note in source["notes"]), source["notes"])

    # -- the read-only promise -------------------------------------------

    def snapshot_of_tree(self):
        """Size and mtime of everything the bridge reads from the checkout."""
        seen = {}
        for path in sorted((ROOT / "workspace/lib").rglob("*")):
            if path.is_file():
                stat = path.stat()
                seen[str(path)] = (stat.st_size, stat.st_mtime_ns)
        return seen

    def test_reading_changes_nothing_in_the_tree(self):
        before = self.snapshot_of_tree()
        for args in (("overview",), ("groups",), ("campaigns",), ("preview", "alpha")):
            self.assertEqual(self.run_bridge(*args, "--date", DAY).returncode, 0)
        self.assertEqual(self.snapshot_of_tree(), before)

    def test_reading_never_records_a_cap_snapshot(self):
        """promo_caps.plan writes the ad-ref history when asked to record. The
        bridge must never ask: a read must not move the rotation on."""
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "ad-ref-history.json"
            refs = Path(directory) / "ad-refs.json"
            for args in (("groups",), ("overview",), ("preview", "alpha")):
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

    def test_a_saved_change_always_reaches_the_campaigns_now(self):
        # Before the cutover a write landed in Firestore while runs still acted
        # on the committed file, and a console had to be able to say so. The
        # file is gone, so there is no longer a state in which a saved change
        # does not reach production.
        self.assertIs(self.overview_writes()["affectsProduction"], True)
        self.assertIs(self.overview_writes(
            GENAIE_CONFIG_SOURCE="file")["affectsProduction"], True)

    def test_cap_settings_come_in_the_shape_a_form_edits(self):
        settings = self.ok("overview", "--date", DAY)["capSettings"]
        self.assertEqual(set(settings), {"enabled", "defaultActivity", "bands"})
        self.assertIs(settings["enabled"], promo_membership.caps_enabled(self.config))
        self.assertEqual(settings["bands"], dict(
            (tier, [low, high]) for tier, (low, high)
            in promo_membership.bands(self.config).items()))

    def test_ad_assets_report_the_one_source_there_is(self):
        assets = self.ok("campaigns", "--date", DAY)["adAssets"]
        self.assertEqual(assets["mode"], "r2")
        self.assertIsNone(assets["warning"])


class UnverifiedScheduleTests(BridgeCase):
    """A campaign whose cron the reconciler has not confirmed."""

    @property
    def CONFIG(self):
        config = promo_fixture.membership()
        config["promos"]["beta"] = promo_fixture.campaign(
            "beta", "13:21", prefix="BE", status="pending")
        return config

    def test_a_campaign_reports_both_switches(self):
        # beta has no verified cron, so it cannot post whatever its store switch
        # says; the console must not offer a toggle as if it could.
        campaigns = dict((row["slug"], row) for row
                         in self.ok("campaigns", "--date", DAY)["campaigns"])
        self.assertIs(campaigns["beta"]["storeEnabled"], True)
        self.assertIs(campaigns["beta"]["cronEnabled"], False)
        self.assertIs(campaigns["beta"]["enabled"], False)
        self.assertIs(campaigns["alpha"]["cronEnabled"], True)
        self.assertIs(campaigns["alpha"]["enabled"], True)
        for row in campaigns.values():
            self.assertIs(row["enabled"], row["storeEnabled"] and row["cronEnabled"],
                          row["slug"])

    def test_an_unverified_campaign_is_never_predicted_to_send(self):
        for row in self.ok("groups", "--date", DAY)["groups"]:
            self.assertNotIn("beta", row["allowedToday"], row["jid"])


class AssetEntryTests(unittest.TestCase):
    """The join itself, in-process. The subprocess tests above drive it through
    an index snapshot; this pins the shape of one entry without a file at all."""

    def setUp(self):
        self.console = load_console_read()
        self.entries = [
            {"id": "alpha-1", "campaign": "alpha", "filename": "one.jpg",
             "key": "ads/alpha/%s.jpg" % ("a" * 64), "sha256": "a" * 64,
             "bytes": 100, "content_type": "image/jpeg", "rank": 10},
            {"id": "alpha-2", "campaign": "alpha", "filename": "two.jpg",
             "key": "ads/alpha/%s.jpg" % ("b" * 64), "sha256": "b" * 64,
             "bytes": 200, "content_type": "image/jpeg", "rank": 20},
        ]
        self.console._CATALOGUE["value"] = {
            "campaigns": {"alpha": [dict(entry, url="%s/%s" % (BASE_URL, entry["key"]))
                                    for entry in self.entries]},
            "source": "snapshot", "fetchedAt": "2026-11-06T06:00:00Z", "note": None}

    def test_a_campaigns_pictures_carry_the_address_of_their_bytes(self):
        entries = self.console.asset_entries("alpha")
        self.assertEqual(len(entries), 2)
        for entry in entries:
            self.assertEqual(set(entry), ASSET_KEYS)
            self.assertTrue(entry["url"].startswith(BASE_URL + "/ads/alpha/"))
            self.assertIn(entry["sha256"], entry["url"])

    def test_the_index_order_is_kept_because_it_is_the_rotation_order(self):
        entries = self.console.asset_entries("alpha")
        self.assertEqual([entry["filename"] for entry in entries],
                         ["one.jpg", "two.jpg"])
        self.assertEqual([entry["rank"] for entry in entries], [10, 20])

    def test_a_campaign_the_index_does_not_mention_has_no_pictures_not_an_error(self):
        self.assertEqual(self.console.asset_entries("beta"), [])

    def test_the_key_never_reaches_the_console_because_the_url_already_has_it(self):
        for entry in self.console.asset_entries("alpha"):
            self.assertNotIn("key", entry)


class PausedAssignmentTests(unittest.TestCase):
    """One group's assignment paused, in-process: the store keeps the campaign
    on the group so the console can show what resuming would restore, and
    nothing may plan a send for it."""

    def setUp(self):
        self.day = promo_fixture.DAY
        self.console = load_console_read()
        self.config = promo_fixture.membership()
        # group 7 in the fixture has alpha configured and alpha paused.
        self.paused = next(jid for jid, entry in self.config["groups"].items()
                           if "alpha" in (entry.get("paused_promos") or []))
        switches = promo_fixture.switches(self.config)
        self.console._RESOLVED["config"] = {
            "membership": self.config, "switches": switches,
            "storeSwitches": switches, "cronSwitches": dict.fromkeys(switches, True),
            "source": "snapshot", "mode": "firestore", "deleted": [],
            "report": {"quarantined": {}, "warnings": [], "campaigns": {}},
            "notes": []}
        self.console._CATALOGUE["value"] = {"campaigns": {}, "source": None,
                                            "fetchedAt": None, "note": "no index"}

    def test_a_paused_group_stays_in_the_campaigns_list_marked_paused(self):
        row = next(row for row in self.console.preview("alpha", self.day)["groups"]
                   if row["jid"] == self.paused)
        self.assertIs(row["paused"], True)
        self.assertIs(row["sendsToday"], False)

    def test_the_group_row_lists_the_pause_and_plans_no_send_for_it(self):
        row = next(row for row in self.console.group_rows(self.day)
                   if row["jid"] == self.paused)
        self.assertIn("alpha", row["promos"])
        self.assertIn("alpha", row["pausedPromos"])
        self.assertNotIn("alpha", row["allowedToday"])


if __name__ == "__main__":
    unittest.main()
