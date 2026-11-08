"""deploy/ad-assets.json must be an honest mirror of what the bot will send.

Firestore is canonical, and a mirror that is allowed to drift from it is worse
than no mirror at all. Two gates keep it honest: ``upload-ad-image.py --verify``
checks it against the store (that needs credentials, so an operator runs it),
and this checks it against the repo, offline, on every push:

* every entry validates through the same code the runtime validates with,
* every entry's digest and size match the image actually baked into this image,
* the rotation order matches the order each campaign's post script declares, so
  seeding cannot silently shift which picture goes out on which day,
* the set of campaigns matches deploy/promo-crons.json.

The digest-and-size checks go away with the baked files in the deletion PR; the
rest stay.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes it.
"""

import hashlib
import importlib.machinery
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT / "workspace/lib"), str(ROOT / "deploy")):
    if path not in sys.path:
        sys.path.insert(0, path)

import ad_assets  # noqa: E402

MIRROR = ROOT / "deploy" / "ad-assets.json"
MANIFEST = ROOT / "deploy" / "promo-crons.json"


def upload_module():
    loader = importlib.machinery.SourceFileLoader(
        "upload_ad_image", str(ROOT / "deploy" / "upload-ad-image.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


UPLOAD = upload_module()
MIRROR_DATA = json.loads(MIRROR.read_text(encoding="utf-8"))
CAMPAIGNS = MIRROR_DATA["campaigns"]
ENTRIES = [e for entries in CAMPAIGNS.values() for e in entries]


class ShapeTest(unittest.TestCase):
    def test_the_mirror_is_not_empty(self):
        self.assertEqual(len(CAMPAIGNS), 10)
        self.assertEqual(len(ENTRIES), 17)

    def test_every_entry_validates_the_way_the_runtime_validates(self):
        for item in ENTRIES:
            ad_assets._entry(item["id"], item)

    def test_campaign_slugs_match_the_cron_manifest(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        declared = set(job["slug"] for job in manifest["promos"])
        self.assertTrue(set(CAMPAIGNS) <= declared,
                        set(CAMPAIGNS) - declared)

    def test_every_entry_sits_under_its_own_campaign(self):
        for slug, entries in CAMPAIGNS.items():
            for item in entries:
                self.assertEqual(item["campaign"], slug)
                self.assertTrue(item["key"].startswith("ads/%s/" % slug), item["key"])

    def test_document_ids_filenames_and_ranks_are_unique_per_campaign(self):
        self.assertEqual(len(set(e["id"] for e in ENTRIES)), len(ENTRIES))
        for slug, entries in CAMPAIGNS.items():
            for field in ("filename", "rank"):
                values = [e[field] for e in entries]
                self.assertEqual(len(set(values)), len(values), (slug, field))

    def test_ids_are_the_house_slug_and_the_rotation_slot(self):
        for item in ENTRIES:
            self.assertEqual(item["id"], "%s-%d"
                             % (item["campaign"], item["rank"] // 10))
            # The house slug, which membership.json and promo_config.promo_key
            # both use: "cv", never "cv-promo".
            self.assertFalse(item["campaign"].endswith("-promo"))

    def test_every_entry_is_enabled(self):
        # Nothing has been retired yet; a disabled entry arriving without a
        # commit message explaining it would be a mistake.
        for item in ENTRIES:
            self.assertTrue(item["enabled"], item["id"])


class BakedFileTest(unittest.TestCase):
    """Delete this class in the PR that deletes the images from the repo."""

    def setUp(self):
        self.dirs = UPLOAD.campaign_dirs()

    def baked(self, item):
        return (self.dirs[item["campaign"]] / ad_assets.ASSETS_DIRNAME
                / item["filename"])

    def test_every_entry_matches_the_image_baked_into_this_repo(self):
        for item in ENTRIES:
            path = self.baked(item)
            self.assertTrue(path.is_file(), path)
            body = path.read_bytes()
            self.assertEqual(len(body), item["bytes"], item["id"])
            self.assertEqual(hashlib.sha256(body).hexdigest(), item["sha256"],
                             item["id"])

    def test_the_key_is_content_addressed_from_that_digest(self):
        for item in ENTRIES:
            self.assertEqual(item["key"], "ads/%s/%s%s"
                             % (item["campaign"], item["sha256"],
                                ad_assets.CONTENT_TYPES[item["content_type"]]))

    def test_every_baked_image_is_indexed(self):
        indexed = set((e["campaign"], e["filename"]) for e in ENTRIES)
        for slug, directory in self.dirs.items():
            assets = directory / ad_assets.ASSETS_DIRNAME
            if not assets.is_dir():
                continue
            for path in assets.iterdir():
                if path.is_file() and ad_assets.FILENAME_RE.match(path.name):
                    self.assertIn((slug, path.name), indexed)

    def test_the_rank_order_matches_what_each_post_script_declares(self):
        # Rotation is by date ordinal over the list, so re-ordering shifts which
        # picture a group sees. cv and genaie-leads both declare an order that is
        # not alphabetical, which is exactly how a sorted seed would break them.
        for slug, directory in self.dirs.items():
            declared = [n for n in UPLOAD.declared_order(directory)
                        if (slug, n) in set((e["campaign"], e["filename"])
                                            for e in ENTRIES)]
            ranked = [e["filename"] for e in
                      sorted(CAMPAIGNS.get(slug) or [], key=lambda e: e["rank"])]
            self.assertEqual(ranked[:len(declared)], declared, slug)

    def test_regenerating_the_mirror_from_the_repo_reproduces_it(self):
        # The file is generated; if this fails, run
        # deploy/upload-ad-image.py --seed-from-repo and commit the result.
        self.assertEqual(UPLOAD.from_repo(self.dirs), CAMPAIGNS)


if __name__ == "__main__":
    unittest.main()
