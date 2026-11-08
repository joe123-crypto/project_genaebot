"""How the upload tool assigns document ids and rotation slots.

Every case here comes from review feedback on the PR that introduced the tool,
and each was a real way to lose a picture:

* a document id derived from ``rank // RANK_STEP`` made rank 10 and rank 15 the
  same document, so one write silently overwrote the other;
* re-uploading a file under a name already in the index gave the replacement a
  fresh id, leaving the old document in Firestore still enabled, so production
  rotated the obsolete artwork alongside the new one;
* nothing checked for a duplicate id, rank or filename before writing.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes it.
"""

import importlib.machinery
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT / "workspace/lib"), str(ROOT / "deploy")):
    if path not in sys.path:
        sys.path.insert(0, path)

import ad_assets  # noqa: E402


def upload_module():
    loader = importlib.machinery.SourceFileLoader(
        "upload_ad_image", str(ROOT / "deploy" / "upload-ad-image.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


TOOL = upload_module()
PNG = b"\x89PNG\r\n\x1a\n" + b"first"
PNG2 = b"\x89PNG\r\n\x1a\n" + b"second"
PNG3 = b"\x89PNG\r\n\x1a\n" + b"third"


class Harness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def image(self, name, body):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as handle:
            handle.write(body)
        return path

    def seeded(self):
        """A campaign with two pictures, as --seed-from-repo would leave it."""
        mirror = {"campaigns": {}}
        return TOOL.with_files(mirror, "cv",
                               [self.image("cv-ad.png", PNG),
                                self.image("cv-ad-2.png", PNG2)])

    def by_name(self, campaigns, name):
        return next(e for e in campaigns["cv"] if e["filename"] == name)


class IdAllocationTest(Harness):
    def test_ids_are_allocated_from_one_upward(self):
        campaigns = self.seeded()
        self.assertEqual([e["id"] for e in campaigns["cv"]], ["cv-1", "cv-2"])
        self.assertEqual([e["rank"] for e in campaigns["cv"]], [10, 20])

    def test_an_intermediate_rank_does_not_collide_with_an_existing_id(self):
        # rank // RANK_STEP gave both of these the id "cv-1".
        campaigns = TOOL.with_files(self.seeded(), "cv",
                                    [self.image("cv-ad-3.png", PNG3)], rank=15)
        third = self.by_name(campaigns, "cv-ad-3.png")
        self.assertEqual(third["rank"], 15)
        self.assertNotIn(third["id"], ("cv-1", "cv-2"))
        self.assertEqual(len(set(e["id"] for e in campaigns["cv"])), 3)

    def test_any_rank_keeps_ids_unique(self):
        campaigns = self.seeded()
        for number, rank in enumerate((1, 11, 12, 13, 999), start=3):
            campaigns = TOOL.with_files(
                campaigns, "cv",
                [self.image("extra-%d.png" % number, PNG + bytes([number]))],
                rank=rank)
        ids = [e["id"] for e in campaigns["cv"]]
        self.assertEqual(len(set(ids)), len(ids))
        TOOL.validate_campaign("cv", campaigns["cv"])

    def test_allocate_id_fills_a_gap_rather_than_reusing_one(self):
        self.assertEqual(TOOL.allocate_id("cv", set()), "cv-1")
        self.assertEqual(TOOL.allocate_id("cv", {"cv-1", "cv-3"}), "cv-2")
        self.assertEqual(TOOL.allocate_id("cv", {"cv-1", "cv-2"}), "cv-3")


class SameFilenameTest(Harness):
    def test_a_new_version_of_a_picture_keeps_its_id_and_its_slot(self):
        before = self.seeded()
        original = self.by_name(before, "cv-ad.png")
        after = TOOL.with_files(before, "cv", [self.image("cv-ad.png", PNG3)])
        updated = self.by_name(after, "cv-ad.png")
        self.assertEqual(updated["id"], original["id"])
        self.assertEqual(updated["rank"], original["rank"])
        self.assertNotEqual(updated["sha256"], original["sha256"])
        self.assertEqual(len(after["cv"]), 2)

    def test_a_new_version_leaves_no_stray_document_behind(self):
        # The failure this prevents: a fresh id for the replacement, the old
        # document still in Firestore and still enabled, and production rotating
        # both the obsolete and the new artwork until --verify caught it.
        before = self.seeded()
        existing = dict((e["id"], TOOL.fields_of(e)) for e in before["cv"])
        after = TOOL.with_files(before, "cv", [self.image("cv-ad.png", PNG3)])
        writes, strays = TOOL.plan(after, existing)
        self.assertEqual(strays, [])
        self.assertEqual([doc_id for _c, doc_id, _f, _e in writes], ["cv-1"])

    def test_the_original_mirror_is_not_mutated(self):
        before = self.seeded()
        snapshot = [dict(e) for e in before["cv"]]
        TOOL.with_files(before, "cv", [self.image("cv-ad.png", PNG3)])
        self.assertEqual(before["cv"], snapshot)


class ReplaceTest(Harness):
    def test_replace_takes_over_the_id_and_slot_of_the_named_image(self):
        before = self.seeded()
        target = self.by_name(before, "cv-ad.png")
        after = TOOL.with_files(before, "cv", [self.image("cv-new.png", PNG3)],
                                replace="cv-ad.png")
        replacement = self.by_name(after, "cv-new.png")
        self.assertEqual(replacement["id"], target["id"])
        self.assertEqual(replacement["rank"], target["rank"])
        self.assertEqual(len(after["cv"]), 2)
        self.assertNotIn("cv-ad.png", [e["filename"] for e in after["cv"]])

    def test_replace_leaves_no_stray_document(self):
        before = self.seeded()
        existing = dict((e["id"], TOOL.fields_of(e)) for e in before["cv"])
        after = TOOL.with_files(before, "cv", [self.image("cv-new.png", PNG3)],
                                replace="cv-ad.png")
        _writes, strays = TOOL.plan(after, existing)
        self.assertEqual(strays, [])

    def test_replacing_an_unknown_filename_is_refused(self):
        with self.assertRaisesRegex(TOOL.UploadError, "no indexed image"):
            TOOL.with_files(self.seeded(), "cv", [self.image("x.png", PNG3)],
                            replace="absent.png")


class ValidationTest(Harness):
    def entry(self, **overrides):
        base = {"id": "cv-1", "campaign": "cv", "filename": "a.png",
                "key": "k", "sha256": "s", "bytes": 1,
                "content_type": "image/png", "rank": 10, "enabled": True}
        base.update(overrides)
        return base

    def test_duplicate_ids_ranks_and_filenames_are_each_refused(self):
        for second in ({"id": "cv-1", "filename": "b.png", "rank": 20},
                       {"id": "cv-2", "filename": "b.png", "rank": 10},
                       {"id": "cv-2", "filename": "a.png", "rank": 20}):
            with self.assertRaises(TOOL.UploadError):
                TOOL.validate_campaign("cv", [self.entry(), self.entry(**second)])

    def test_a_clean_campaign_passes(self):
        TOOL.validate_campaign("cv", [self.entry(),
                                      self.entry(id="cv-2", filename="b.png",
                                                 rank=20)])

    def test_a_disabled_image_keeps_its_id_and_slot(self):
        after = TOOL.with_files(self.seeded(), "cv", [], disable=["cv-ad.png"])
        disabled = self.by_name(after, "cv-ad.png")
        self.assertFalse(disabled["enabled"])
        self.assertEqual((disabled["id"], disabled["rank"]), ("cv-1", 10))

    def test_disabling_an_unknown_filename_is_refused(self):
        with self.assertRaisesRegex(TOOL.UploadError, "no indexed image"):
            TOOL.with_files(self.seeded(), "cv", [], disable=["absent.png"])


class DescribeTest(Harness):
    def test_the_entry_validates_through_the_runtimes_own_rules(self):
        entry, body = TOOL.describe("cv", self.image("cv-ad.png", PNG), 10, "cv-1")
        self.assertEqual(body, PNG)
        self.assertEqual(ad_assets._entry(entry["id"], entry)["filename"],
                         "cv-ad.png")
        self.assertEqual(entry["key"], "ads/cv/%s.png" % entry["sha256"])

    def test_a_file_that_is_not_a_jpeg_or_png_is_refused(self):
        with self.assertRaisesRegex(TOOL.UploadError, "not a JPEG or a PNG"):
            TOOL.describe("cv", self.image("cv-ad.png", b"GIF89a junk"), 10, "cv-1")

    def test_an_oversized_file_is_refused(self):
        big = b"\x89PNG\r\n\x1a\n" + b"x" * ad_assets.MAX_BYTES
        with self.assertRaisesRegex(TOOL.UploadError, "over the"):
            TOOL.describe("cv", self.image("cv-ad.png", big), 10, "cv-1")

    def test_an_empty_file_is_refused(self):
        with self.assertRaisesRegex(TOOL.UploadError, "is empty"):
            TOOL.describe("cv", self.image("cv-ad.png", b""), 10, "cv-1")


if __name__ == "__main__":
    unittest.main()
