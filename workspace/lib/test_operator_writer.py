"""Writing operator config to Firestore (workspace/lib/operator_writer.py).

The claim that matters: a change is judged by the SAME code that decides
whether a campaign run may post to a group, so the console cannot save a group
into a state that silently stops it being advertised to. Everything else here
is the refusal surface -- unknown fields, rank, a config-wide break -- and the
guarantee that a change and its audit record land together.

The fake Firestore from test_operator_config answers reads over the same
``opener`` seam ``urllib`` uses; this adds ``:commit``, so the whole path runs
under ``--network none`` with no emulator.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes it.
"""

import copy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.parse

LIB = os.path.dirname(os.path.abspath(__file__))
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import operator_config as oc  # noqa: E402
import operator_writer as ow  # noqa: E402
from test_operator_config import (FakeFirestore, JID_A, JID_B, PROJECT,  # noqa: E402
                                  Response, creds_b64, membership, seed_store)


class WritableFirestore(FakeFirestore):
    """The read fake plus ``documents:commit``, applied to its own store so a
    test can read back exactly what a write would leave behind."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.commits = []
        self.commit_status = None     # e.g. 403 for a key without write access

    def __call__(self, request, timeout=None):
        if not request.full_url.endswith(":commit"):
            return super().__call__(request, timeout=timeout)
        self.requests.append(request.full_url)
        if self.down:
            raise urllib.error.URLError("connection refused")
        if self.commit_status:
            raise urllib.error.HTTPError(request.full_url, self.commit_status,
                                         "no", {}, io.BytesIO())
        assert request.get_header("Authorization", "").startswith("Bearer tok-")
        body = json.loads(request.data)
        self.commits.append(body)
        for write in body["writes"]:
            if "delete" in write:
                collection, doc_id = write["delete"].rsplit("/", 2)[-2:]
                self.store.get(urllib.parse.unquote(collection), {}).pop(
                    urllib.parse.unquote(doc_id), None)
                continue
            name = write["update"]["name"]
            collection, doc_id = name.rsplit("/", 2)[-2:]
            self.store.setdefault(urllib.parse.unquote(collection), {})[
                urllib.parse.unquote(doc_id)] = write["update"]["fields"]
        return Response({"writeResults": [{} for _ in body["writes"]]})


class Harness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env = {"GENAIE_FIRESTORE_DIR": os.path.join(self.dir, "fs"),
                    "GCP_SERVICE_ACCOUNT_B64": creds_b64()}
        self.firestore = WritableFirestore(page_size=50)
        self.clock = [1_800_000_000.0]

    def write(self, collection, doc_id, patch, actor="joe", **kw):
        return ow.write(collection, doc_id, patch, actor, self.env,
                        opener=self.firestore,
                        signer=lambda header, claims, key: "jwt",
                        now=lambda: self.clock[0], sleep=lambda s: None, **kw)

    def stored(self, collection, doc_id):
        return oc.decode_fields(self.firestore.store[collection][doc_id])

    def raw(self):
        return dict((collection, list(docs.items()))
                    for collection, docs in self.firestore.store.items())


class AppliedChanges(Harness):
    def test_a_status_change_is_written_and_nothing_else_moves(self):
        before = self.stored("groups", JID_A)
        result = self.write("groups", JID_A, {"status": "on-hold"})
        self.assertEqual(result["change"], "updated")
        self.assertEqual(result["fields"], ["status"])
        after = self.stored("groups", JID_A)
        self.assertEqual(after["status"], "on-hold")
        self.assertEqual(oc._without(after, "status"), oc._without(before, "status"))

    def test_pausing_a_group_keeps_its_campaigns(self):
        # Status is what gates sending; the promos list is the intent, and
        # keeping it is what makes resuming a group a status flip rather than a
        # rediscovery of which campaigns it belonged to.
        promos = self.stored("groups", JID_B)["promos"]
        self.write("groups", JID_B, {"status": "on-hold"})
        self.assertEqual(self.stored("groups", JID_B)["promos"], promos)

    def test_rank_survives_a_change(self):
        rank = self.stored("groups", JID_A)["rank"]
        self.write("groups", JID_A, {"note": "touched"})
        self.assertEqual(self.stored("groups", JID_A)["rank"], rank)

    def test_a_null_is_stored_as_a_null_not_dropped(self):
        # daily_cap: null means "use the band", which is not the same as absent.
        self.write("groups", JID_A, {"daily_cap": None})
        self.assertIn("daily_cap", self.stored("groups", JID_A))
        self.assertIsNone(self.stored("groups", JID_A)["daily_cap"])

    def test_an_unchanged_change_is_not_committed(self):
        status = self.stored("groups", JID_A)["status"]
        result = self.write("groups", JID_A, {"status": status})
        self.assertEqual(result["change"], "unchanged")
        self.assertEqual(self.firestore.commits, [])

    def test_a_dry_run_reports_the_change_and_writes_nothing(self):
        result = self.write("groups", JID_A, {"status": "on-hold"}, dry_run=True)
        self.assertEqual(result["fields"], ["status"])
        self.assertEqual(self.firestore.commits, [])
        self.assertNotEqual(self.stored("groups", JID_A)["status"], "on-hold")

    def test_a_new_group_is_appended_after_the_current_last(self):
        # Order is send order, so appending is the one position that cannot
        # change who an existing short run reaches.
        ranks = [self.stored("groups", jid)["rank"]
                 for jid in self.firestore.store["groups"]]
        new = "263700000000-1600000000@g.us"
        self.write("groups", new,
                   {"name": "New Group", "source": "operator", "status": "active",
                    "promos": [], "daily_cap": None})
        self.assertEqual(self.stored("groups", new)["rank"], int(max(ranks)) + ow.RANK_STEP)

    def test_a_group_can_be_paused_for_one_campaign(self):
        # Bravo is in genaie and cv; pausing cv keeps both assignments.
        result = self.write("groups", JID_B, {"paused_promos": ["cv"]})
        self.assertEqual(result["fields"], ["paused_promos"])
        stored = self.stored("groups", JID_B)
        self.assertEqual(stored["paused_promos"], ["cv"])
        self.assertEqual(stored["promos"], ["genaie", "cv"])

    def test_removing_an_assignment_clears_its_pause_and_cadence_in_one_write(self):
        # Bravo is in cv on a weekly cadence. Removing cv has to take every
        # reference to it out together -- promos, paused_promos and weekly --
        # because any one left behind names a campaign the group is not in.
        self.write("groups", JID_B, {"paused_promos": ["cv"]})
        self.write("groups", JID_B,
                   {"promos": ["genaie"], "paused_promos": [], "weekly": {}})
        stored = self.stored("groups", JID_B)
        self.assertEqual((stored["promos"], stored["paused_promos"], stored["weekly"]),
                         (["genaie"], [], {}))

    def test_a_campaign_can_be_switched_off(self):
        self.write("campaigns", "cv", {"enabled": False})
        self.assertIs(self.stored("campaigns", "cv")["enabled"], False)

    def test_a_group_can_be_added_beside_one_that_cannot_be_read(self):
        # The Firebase Console has no schema, so a document that will not decode
        # is a real state. It is quarantined for the run and must not stop an
        # unrelated change -- least of all by crashing the rank calculation.
        self.firestore.store["groups"][JID_B]["name"] = {
            "timestampValue": "2026-11-04T00:00:00Z"}
        new = "263700000000-1600000000@g.us"
        self.write("groups", new,
                   {"name": "New Group", "source": "operator", "status": "active",
                    "promos": [], "daily_cap": None})
        self.assertIn(new, self.firestore.store["groups"])

    def test_the_unreadable_document_itself_cannot_be_patched_blind(self):
        # Patching one field of a document we cannot read would write back a
        # document built from a guess at the rest of it.
        self.firestore.store["groups"][JID_A]["name"] = {
            "timestampValue": "2026-11-04T00:00:00Z"}
        with self.assertRaises(ow.WriteRefused) as caught:
            self.write("groups", JID_A, {"status": "on-hold"})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.firestore.commits, [])


class Refusals(Harness):
    def refuse(self, *args, **kw):
        with self.assertRaises(ow.WriteRefused) as caught:
            self.write(*args, **kw)
        self.assertEqual(self.firestore.commits, [])
        return caught.exception

    def test_a_status_the_bot_does_not_know_is_refused(self):
        ex = self.refuse("groups", JID_A, {"status": "paused"})
        self.assertTrue(ex.problems)

    def test_pausing_a_campaign_the_group_is_not_in_is_refused(self):
        # Alpha is only in genaie. A pause for cv would quarantine the group.
        ex = self.refuse("groups", JID_A, {"paused_promos": ["cv"]})
        self.assertTrue(any("paused_promos" in p for p in ex.problems))

    def test_removing_an_assignment_but_leaving_it_paused_is_refused(self):
        # The console must unpause in the same write; the bot will not guess.
        self.write("groups", JID_B, {"paused_promos": ["cv"]})
        self.firestore.commits.clear()
        self.refuse("groups", JID_B, {"promos": ["genaie"], "weekly": {}})

    def test_removing_an_assignment_but_leaving_its_cadence_is_refused(self):
        # Bravo's cv runs weekly; the cadence has to go with the assignment.
        self.refuse("groups", JID_B, {"promos": ["genaie"]})

    def test_a_caps_switch_that_is_not_a_boolean_is_refused(self):
        # bool("false") is True: the reader would switch caps ON.
        ex = self.refuse("settings", "defaults", {"caps_enabled": "false"})
        self.assertIn("caps_enabled must be true or false", ex.problems[0])

    def test_a_band_the_reader_would_ignore_is_refused(self):
        # A malformed band silently falls back to the default when read.
        for bands in ({"busy": [10, 6]}, {"busy": [6]}, {"busy": [6.5, 10]},
                      {"busy": [-1, 3]}, {}):
            with self.subTest(bands=bands):
                self.refuse("settings", "defaults", {"bands": bands})

    def test_a_default_tier_without_a_band_is_refused(self):
        self.refuse("settings", "defaults", {"activity": "frantic"})

    def test_a_built_in_tier_is_a_valid_default_when_bands_hold_only_overrides(self):
        # Stored bands are overrides: the reader fills in the built-in tiers, and
        # the console offers them, so choosing one must be accepted.
        self.firestore.store["settings"]["defaults"]["bands"] = oc.encode_value({"busy": [6, 10]})
        self.write("settings", "defaults", {"activity": "quiet"})
        self.assertEqual(self.stored("settings", "defaults")["activity"], "quiet")
        self.firestore.commits.clear()
        self.refuse("settings", "defaults", {"activity": "frantic"})

    def test_a_tier_added_in_the_same_change_is_a_valid_default(self):
        self.write("settings", "defaults", {"bands": {"busy": [6, 10], "normal": [4, 6],
                                                      "quiet": [1, 3], "dormant": [0, 1]},
                                            "activity": "dormant"})
        self.assertEqual(self.stored("settings", "defaults")["activity"], "dormant")

    def test_a_campaign_switch_that_is_not_a_boolean_is_refused(self):
        # A non-boolean reads as OFF: saving "true" would stop the campaign.
        ex = self.refuse("campaigns", "cv", {"enabled": "true"})
        self.assertIn("enabled must be true or false", ex.problems[0])

    def test_valid_cap_settings_are_written(self):
        result = self.write("settings", "defaults",
                            {"caps_enabled": False, "activity": "quiet",
                             "bands": {"busy": [5, 9], "normal": [3, 5], "quiet": [1, 2]}})
        self.assertEqual(sorted(result["fields"]), ["activity", "bands", "caps_enabled"])
        self.assertIs(self.stored("settings", "defaults")["caps_enabled"], False)

    def test_a_bad_value_already_stored_does_not_block_an_unrelated_edit(self):
        # Only the fields a change names are judged: garbage typed into the
        # Firebase Console earlier must not make every later edit fail.
        self.firestore.store["settings"]["defaults"]["caps_enabled"] = oc.encode_value("yes")
        self.write("settings", "defaults", {"activity": "quiet"})
        self.assertEqual(self.stored("settings", "defaults")["activity"], "quiet")

    def test_an_unknown_field_is_refused_by_name(self):
        ex = self.refuse("groups", JID_A, {"stattus": "on-hold"})
        self.assertIn("stattus", str(ex))
        self.assertIn("editable:", str(ex))

    def test_rank_is_refused_with_a_reason(self):
        ex = self.refuse("groups", JID_A, {"rank": 5})
        self.assertIn("send order", str(ex))

    def test_an_empty_change_is_refused(self):
        self.refuse("groups", JID_A, {})

    def test_a_group_pointed_at_a_line_that_does_not_exist_is_refused(self):
        # Posting from the wrong number is not safe, so this one must not save.
        self.refuse("groups", JID_A, {"account": "sales"})

    def test_a_campaign_a_group_still_lists_cannot_be_renamed_away(self):
        # Clearing the contact account every caption needs would break every
        # campaign, so a settings change is judged against every group.
        self.refuse("settings", "defaults", {"contact_account": "nobody"})

    def test_a_value_firestore_cannot_hold_is_refused(self):
        self.refuse("groups", JID_A, {"note": {"nested": object()}})

    def test_an_unknown_collection_is_a_404(self):
        ex = self.refuse("secrets", "x", {"a": 1})
        self.assertEqual(ex.status, 404)

    def test_a_line_cannot_be_created_through_this_path(self):
        # A new line needs a WhatsApp login, not a form.
        ex = self.refuse("lines", "sales", {"number": "+263700000000"})
        self.assertEqual(ex.status, 404)

    def test_an_outage_means_nothing_is_written(self):
        self.firestore.down = True
        with self.assertRaises(oc.ConfigUnavailable):
            self.write("groups", JID_A, {"status": "on-hold"})
        self.assertEqual(self.firestore.commits, [])

    def test_a_config_read_429_writes_neither_the_group_nor_an_audit(self):
        self.write("groups", JID_A, {"status": "on-hold"}, dry_run=True)
        before = self.stored("groups", JID_A)
        self.firestore.fail_next = [429, 429]
        with self.assertRaises(oc.ConfigUnavailable) as caught:
            self.write("groups", JID_A, {"activity": "busy"})
        self.assertEqual(caught.exception.metadata["code"], "config_rate_limited")
        self.assertEqual(self.stored("groups", JID_A), before)
        self.assertEqual(self.firestore.commits, [])

    def test_a_read_only_key_fails_at_the_commit_without_half_applying(self):
        self.firestore.commit_status = 403
        with self.assertRaises(oc.ConfigUnavailable) as caught:
            self.write("groups", JID_A, {"status": "on-hold"})
        self.assertIn("permission denied", str(caught.exception))
        self.assertNotEqual(self.stored("groups", JID_A)["status"], "on-hold")


class Audit(Harness):
    def test_a_change_and_its_record_land_in_one_commit(self):
        # Firestore keeps no history of a write and the Firebase Console bypasses
        # this path, so this is the only trail there is: it must not be possible
        # for the change to land without it.
        result = self.write("groups", JID_A, {"status": "on-hold"})
        self.assertEqual(len(self.firestore.commits), 1)
        names = [w["update"]["name"].rsplit("/documents/", 1)[1]
                 for w in self.firestore.commits[0]["writes"]]
        self.assertEqual(sorted(n.split("/")[0] for n in names),
                         ["audit", "groups"])
        self.assertIn(result["audit"], self.firestore.store[ow.AUDIT_COLLECTION])

    def test_the_record_says_who_changed_what_from_what(self):
        before = self.stored("groups", JID_A)["status"]
        result = self.write("groups", JID_A, {"status": "on-hold"}, actor="joe")
        entry = self.stored(ow.AUDIT_COLLECTION, result["audit"])
        self.assertEqual(entry["actor"], "joe")
        self.assertEqual(entry["collection"], "groups")
        self.assertEqual(entry["id"], JID_A)
        self.assertEqual(entry["change"], "updated")
        self.assertEqual(entry["fields"], ["status"])
        self.assertEqual(entry["before"]["status"], before)
        self.assertEqual(entry["after"]["status"], "on-hold")
        self.assertEqual(entry["at"], "2027-01-15T08:00:00Z")

    def test_an_anonymous_change_is_still_recorded(self):
        result = self.write("groups", JID_A, {"note": "x"}, actor=None)
        self.assertEqual(self.stored(ow.AUDIT_COLLECTION, result["audit"])["actor"],
                         "unknown")

    def test_two_changes_in_the_same_second_get_their_own_records(self):
        first = self.write("groups", JID_A, {"note": "one"})
        second = self.write("groups", JID_A, {"note": "two"})
        self.assertNotEqual(first["audit"], second["audit"])
        self.assertEqual(len(self.firestore.store[ow.AUDIT_COLLECTION]), 2)

    def test_the_audit_collection_is_invisible_to_a_campaign_run(self):
        # A reader lists only the four config collections, so the trail can grow
        # without costing every promo run a page of reads.
        self.write("groups", JID_A, {"note": "x"})
        self.assertNotIn(ow.AUDIT_COLLECTION, oc.COLLECTIONS)
        loaded = oc.load(self.env, opener=self.firestore,
                         signer=lambda header, claims, key: "jwt",
                         now=lambda: self.clock[0], sleep=lambda s: None,
                         log=lambda m: None)
        self.assertEqual(loaded["source"], "firestore")


class Deletion(Harness):
    def delete(self, collection, doc_id, **kw):
        kw.setdefault("actor", "joe")
        actor = kw.pop("actor")
        return ow.delete(collection, doc_id, actor, self.env, opener=self.firestore,
                         signer=lambda header, claims, key: "jwt",
                         now=lambda: self.clock[0], sleep=lambda s: None, **kw)

    def test_a_group_and_its_record_leave_in_one_commit(self):
        # The delete and the trail that would let it be recreated land together
        # or not at all; there is no moment where the group is gone unrecorded.
        result = self.delete("groups", JID_A, reason="bot was removed")
        self.assertEqual(len(self.firestore.commits), 1)
        ops = self.firestore.commits[0]["writes"]
        self.assertEqual(sorted(next(iter(op)) for op in ops), ["delete", "update"])
        self.assertNotIn(JID_A, self.firestore.store["groups"])
        self.assertIn(result["audit"], self.firestore.store[ow.AUDIT_COLLECTION])

    def test_the_record_holds_everything_needed_to_recreate_the_group(self):
        before = self.stored("groups", JID_A)
        result = self.delete("groups", JID_A, reason="bot was removed")
        entry = self.stored(ow.AUDIT_COLLECTION, result["audit"])
        self.assertEqual(entry["change"], "deleted")
        self.assertEqual(entry["before"], before)
        self.assertEqual(entry["after"], {})
        self.assertEqual(entry["reason"], "bot was removed")
        self.assertEqual(entry["actor"], "joe")

    def test_a_reason_is_optional(self):
        result = self.delete("groups", JID_A)
        self.assertNotIn("reason", self.stored(ow.AUDIT_COLLECTION, result["audit"]))

    def test_only_groups_can_be_deleted(self):
        # A line named by groups would quarantine them all; a campaign is code;
        # settings is what every group is judged against.
        for collection, doc_id in (("lines", "default"), ("campaigns", "cv"),
                                   ("settings", "defaults")):
            with self.subTest(collection=collection):
                with self.assertRaises(ow.WriteRefused) as caught:
                    self.delete(collection, doc_id)
                self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.firestore.commits, [])

    def test_deleting_a_group_that_is_not_there_is_a_404(self):
        with self.assertRaises(ow.WriteRefused) as caught:
            self.delete("groups", "263700000000-1600000000@g.us")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(self.firestore.commits, [])

    def test_a_group_that_cannot_be_read_cannot_be_deleted_blind(self):
        # Its record could not go into the audit trail, which is the only thing
        # that makes a delete recoverable.
        self.firestore.store["groups"][JID_A]["name"] = {
            "timestampValue": "2026-11-04T00:00:00Z"}
        with self.assertRaises(ow.WriteRefused) as caught:
            self.delete("groups", JID_A)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.firestore.commits, [])

    def test_a_dry_run_reports_and_deletes_nothing(self):
        result = self.delete("groups", JID_A, dry_run=True)
        self.assertEqual(result["change"], "deleted")
        self.assertIsNone(result["audit"])
        self.assertEqual(self.firestore.commits, [])
        self.assertIn(JID_A, self.firestore.store["groups"])

    def test_a_deleted_group_is_no_longer_targeted(self):
        self.delete("groups", JID_A)
        loaded = oc.load(self.env, opener=self.firestore,
                         signer=lambda header, claims, key: "jwt",
                         now=lambda: self.clock[0], sleep=lambda s: None,
                         log=lambda m: None)
        self.assertNotIn(JID_A, loaded["membership"]["groups"])
        self.assertEqual(loaded["report"]["quarantined"], {})


class SharedWithTheSeed(Harness):
    def test_the_seed_and_this_module_shape_documents_identically(self):
        # One definition of a legal document: the seed mirrors the file through
        # the same function the console writes through.
        docs = ow.documents(*_seed_sources())
        self.assertEqual(sorted(docs), sorted(oc.COLLECTIONS))
        self.assertEqual([doc_id for doc_id, _ in docs["settings"]], [oc.SETTINGS_DOC])
        ranks = [data["rank"] for _, data in docs["groups"]]
        self.assertEqual(ranks, sorted(ranks))

    def test_plan_writes_reports_strays_without_touching_them(self):
        desired = {"groups": [("a@g.us", {"name": "A"})]}
        existing = {"groups": [("a@g.us", {"name": "A"}), ("b@g.us", {"name": "B"})]}
        writes, strays, unchanged = ow.plan_writes(desired, existing)
        self.assertEqual((writes, unchanged), ([], 1))
        self.assertEqual(strays, [("groups", "b@g.us")])


def _seed_sources():
    import importlib.util
    from pathlib import Path

    path = Path(LIB).parents[1] / "deploy" / "seed-firestore.py"
    spec = importlib.util.spec_from_file_location("seed_firestore", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_sources()


if __name__ == "__main__":
    unittest.main()
