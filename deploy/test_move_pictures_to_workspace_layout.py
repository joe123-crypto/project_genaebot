#!/usr/bin/env python3
"""Tests for deploy/move-pictures-to-workspace-layout.py.

The plan, the guards and the copy run against plain dicts and a fake bucket;
nothing here reaches Firestore or R2."""

import hashlib
import importlib.machinery
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "workspace" / "lib"))
sys.path.insert(0, str(ROOT / "deploy"))


def load():
    loader = importlib.machinery.SourceFileLoader(
        "move_pictures", str(ROOT / "deploy" / "move-pictures-to-workspace-layout.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


TOOL = load()
WID = "wz_dv8lK0G2QygE2W"
NEW = "workspaces/%s/campaigns/" % WID
JPG = b"\xff\xd8\xff" + b"borehole"
SHA = hashlib.sha256(JPG).hexdigest()
PNG = b"\x89PNG\r\n\x1a\n" + b"web"
SHA2 = hashlib.sha256(PNG).hexdigest()


def asset(campaign, key, body, kind="image/jpeg", filename="a.jpg"):
    return {"campaign": campaign, "filename": filename, "key": key,
            "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
            "content_type": kind, "rank": 10, "enabled": True}


def image(row):
    return {"filename": row["filename"], "key": row["key"], "sha256": row["sha256"],
            "bytes": row["bytes"], "contentType": row["content_type"]}


class Index:
    def __init__(self):
        a = asset("borehole", "ads/borehole/%s.jpg" % SHA, JPG)
        b = asset("web", "w/%s/ads/web/%s.png" % (WID, SHA2), PNG, "image/png", "b.png")
        self.assets = {"borehole-1": a, "web-1": b}
        self.campaigns = {"borehole": {"kind": "console", "images": [image(a)]},
                          "web": {"kind": "console", "images": [image(b)]}}


class KeyTests(unittest.TestCase):
    def test_both_legacy_shapes_keep_their_campaign_folder_and_name(self):
        self.assertEqual(TOOL.new_key("ads/borehole/%s.jpg" % SHA, WID), NEW + "borehole/%s.jpg" % SHA)
        self.assertEqual(TOOL.new_key("w/%s/ads/web/%s.png" % (WID, SHA2), WID), NEW + "web/%s.png" % SHA2)

    def test_a_key_already_moved_needs_nothing(self):
        self.assertIsNone(TOOL.new_key(NEW + "web/%s.png" % SHA2, WID))

    def test_another_workspaces_key_is_never_taken(self):
        for key in ("w/wk-bravo1/ads/web/%s.png" % SHA2, "workspaces/wk-bravo1/campaigns/web/%s.png" % SHA2):
            with self.subTest(key=key), self.assertRaisesRegex(TOOL.MoveError, "another workspace"):
                TOOL.new_key(key, WID)

    def test_a_malformed_key_is_refused(self):
        for key in ("ads/../x/%s.jpg" % SHA, "https://elsewhere/x.jpg", None):
            with self.subTest(key=key), self.assertRaises(TOOL.MoveError):
                TOOL.new_key(key, WID)


class PlanTests(unittest.TestCase):
    def test_every_row_and_every_campaign_image_is_repointed(self):
        index = Index()
        moves, assets, campaigns = TOOL.plan(WID, index.assets, index.campaigns)
        self.assertEqual(sorted(m["key"] for m in moves.values()),
                         sorted([NEW + "borehole/%s.jpg" % SHA, NEW + "web/%s.png" % SHA2]))
        self.assertEqual(assets["borehole-1"][1]["key"], NEW + "borehole/%s.jpg" % SHA)
        self.assertEqual(campaigns["web"][1][0]["key"], NEW + "web/%s.png" % SHA2)
        # The inputs are left as they were: they are the audit's "before".
        self.assertEqual(index.campaigns["web"]["images"][0]["key"], "w/%s/ads/web/%s.png" % (WID, SHA2))

    def test_a_deleted_campaign_is_left_to_its_own_cleanup(self):
        index = Index()
        index.campaigns["web"]["deleted"] = True
        moves, assets, campaigns = TOOL.plan(WID, index.assets, index.campaigns)
        self.assertNotIn("web-1", assets)
        self.assertNotIn("web", campaigns)
        self.assertEqual(len(moves), 1)

    def test_a_moved_index_plans_nothing(self):
        index = Index()
        moves, assets, campaigns = TOOL.plan(WID, index.assets, index.campaigns)
        for doc_id, (_, after) in assets.items():
            index.assets[doc_id] = after
        for slug, (_, after) in campaigns.items():
            index.campaigns[slug]["images"] = after
        self.assertEqual(TOOL.plan(WID, index.assets, index.campaigns), ({}, {}, {}))

    def test_one_key_named_with_two_digests_is_refused(self):
        index = Index()
        index.campaigns["borehole"]["images"][0]["bytes"] += 1
        with self.assertRaisesRegex(TOOL.MoveError, "two different"):
            TOOL.plan(WID, index.assets, index.campaigns)

    def test_a_live_upload_lease_blocks_the_move(self):
        uploads = {"old": {"expiresAt": 100}, "live": {"expiresAt": 300}, "odd": {}}
        self.assertEqual(TOOL.active_leases(uploads, 200), ["live"])

    def test_the_commit_masks_each_field_and_audits_each_document(self):
        index = Index()
        _, assets, campaigns = TOOL.plan(WID, index.assets, index.campaigns)
        writes = TOOL.commit_writes("projects/p/databases/(default)/documents/workspaces/" + WID,
                                    WID, assets, campaigns, now=lambda: 1_800_000_000)
        updates = [w for w in writes if "updateMask" in w]
        self.assertEqual(sorted(w["updateMask"]["fieldPaths"][0] for w in updates),
                         ["images", "images", "key", "key"])
        self.assertTrue(all(w["currentDocument"] == {"exists": True} for w in updates))
        audits = [w for w in writes if "/audit/" in w["update"]["name"]]
        self.assertEqual(len(audits), 4)
        self.assertTrue(all(w["currentDocument"] == {"exists": False} for w in audits))


class Bucket:
    """The four R2 calls the script makes, against a dict."""

    def __init__(self, objects):
        self.objects = dict(objects)
        self.puts, self.deletes = [], []

    def install(self, test):
        for name, fake in (("get_object", self.get), ("put_object", self.put),
                           ("delete_object", self.delete), ("list_objects", self.list)):
            patcher = mock.patch.object(TOOL.r2_sigv4, name, fake)
            patcher.start()
            test.addCleanup(patcher.stop)

    def get(self, endpoint, bucket, key, **kwargs):
        return self.objects[key]

    def put(self, endpoint, bucket, key, body, content_type, **kwargs):
        self.puts.append(key)
        self.objects[key] = body

    def delete(self, endpoint, bucket, key, **kwargs):
        self.deletes.append(key)
        self.objects.pop(key, None)

    def list(self, endpoint, bucket, prefix=None, **kwargs):
        return [{"key": k, "bytes": len(v)} for k, v in sorted(self.objects.items())
                if prefix is None or k.startswith(prefix)]


R2 = ("https://r2.example", "agent-genaie", "access", "secret")


class CopyTests(unittest.TestCase):
    def setUp(self):
        index = Index()
        self.moves, _, _ = TOOL.plan(WID, index.assets, index.campaigns)
        self.bucket = Bucket({"ads/borehole/%s.jpg" % SHA: JPG, "w/%s/ads/web/%s.png" % (WID, SHA2): PNG})
        self.bucket.install(self)

    def test_each_picture_is_copied_once_and_a_rerun_copies_nothing(self):
        present = TOOL.bucket(R2, None)
        self.assertEqual(TOOL.copy_objects(R2, self.moves, present, log=lambda line: None), 2)
        self.assertEqual(self.bucket.objects[NEW + "borehole/%s.jpg" % SHA], JPG)
        present = TOOL.bucket(R2, None)
        self.assertEqual(TOOL.copy_objects(R2, self.moves, present, log=lambda line: None), 0)
        self.assertEqual(len(self.bucket.puts), 2)

    def test_bytes_that_disagree_with_the_index_are_not_copied(self):
        self.bucket.objects["ads/borehole/%s.jpg" % SHA] = JPG + b"tampered"
        present = TOOL.bucket(R2, None)
        with self.assertRaisesRegex(TOOL.MoveError, "does not hold the bytes"):
            TOOL.copy_objects(R2, self.moves, present, log=lambda line: None)

    def test_a_missing_original_stops_the_copy(self):
        del self.bucket.objects["ads/borehole/%s.jpg" % SHA]
        with self.assertRaisesRegex(TOOL.MoveError, "not in the bucket"):
            TOOL.copy_objects(R2, self.moves, TOOL.bucket(R2, None), log=lambda line: None)


class DeleteOldTests(unittest.TestCase):
    def test_only_unnamed_objects_with_a_matching_copy_are_deleted(self):
        legacy = {"ads/borehole/%s.jpg" % SHA: len(JPG),          # copied, unnamed: goes
                  "ads/web/%s.png" % SHA2: len(PNG),              # still named: stays
                  "ads/spare/%s.png" % SHA2: len(PNG),            # never copied: stays
                  "ads/short/%s.jpg" % SHA: len(JPG)}             # copy is the wrong size: stays
        present = {NEW + "borehole/%s.jpg" % SHA: len(JPG), NEW + "web/%s.png" % SHA2: len(PNG),
                   NEW + "short/%s.jpg" % SHA: 1}
        named = {"ads/web/%s.png" % SHA2}
        delete, keep = TOOL.deletable(WID, legacy, present, named)
        self.assertEqual(delete, [("ads/borehole/%s.jpg" % SHA, NEW + "borehole/%s.jpg" % SHA)])
        self.assertEqual(sorted(k for k, _ in keep),
                         sorted(["ads/web/%s.png" % SHA2, "ads/spare/%s.png" % SHA2, "ads/short/%s.jpg" % SHA]))

    def test_names_come_from_rows_of_deleted_campaigns_too(self):
        index = Index()
        index.campaigns["web"]["deleted"] = True
        self.assertIn("w/%s/ads/web/%s.png" % (WID, SHA2), TOOL.named_keys(index.assets, index.campaigns))


if __name__ == "__main__":
    unittest.main()
