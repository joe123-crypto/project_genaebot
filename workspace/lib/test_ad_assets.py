"""Cover ad_assets: the mode gate, index validation and quarantine, the byte
cache and its digest check, partial failure, the snapshot ladder, and that no
mode but ``r2`` ever changes what a run sends.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes them. Fully offline: Firestore, Google's token endpoint and
R2 are one in-memory fake behind the same ``opener`` seam urllib uses, because
the acceptance job runs with ``--network none``.
"""

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ad_assets as aa  # noqa: E402
import operator_config as oc  # noqa: E402
from test_operator_config import FakeFirestore, creds_b64  # noqa: E402

BASE = "https://ads.example.test"
PNG = b"\x89PNG\r\n\x1a\n" + b"cv-ad bytes"
PNG2 = b"\x89PNG\r\n\x1a\n" + b"second cv-ad bytes"
JPG = b"\xff\xd8\xff" + b"third cv-ad bytes"


def digest(body):
    return hashlib.sha256(body).hexdigest()


def entry(campaign, filename, body, rank=10, enabled=True, **overrides):
    kind = "image/png" if filename.endswith(".png") else "image/jpeg"
    out = {"campaign": campaign, "filename": filename,
           "key": "ads/%s/%s%s" % (campaign, digest(body),
                                   ".png" if kind == "image/png" else ".jpg"),
           "sha256": digest(body), "bytes": len(body), "content_type": kind,
           "rank": rank, "enabled": enabled}
    out.update(overrides)
    return out


class Bytes(object):
    """A urllib response carrying bytes, not JSON."""

    def __init__(self, body, content_type):
        self._body = io.BytesIO(body)
        self.headers = {"Content-Type": content_type}
        self.status = 200

    def read(self, size=-1):
        return self._body.read(size) if size and size > 0 else self._body.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class World(object):
    """Firestore and R2 behind one opener, which is how production has it: both
    default to ``urllib.request.urlopen``."""

    def __init__(self, store=None, objects=None):
        self.firestore = FakeFirestore(store={aa.COLLECTION: store or {}})
        self.objects = dict(objects or {})
        self.gets = []
        self.agents = []
        self.missing = set()
        self.down = False
        self.hang = None
        self.serve = {}          # key -> (body, content_type) override

    def __call__(self, request, timeout=None):
        url = request.full_url
        if not url.startswith(BASE):
            return self.firestore(request, timeout=timeout)
        key = url[len(BASE) + 1:]
        self.gets.append(key)
        self.agents.append(request.get_header("User-agent"))
        if self.hang:
            self.hang.wait()
        if self.down or key in self.missing or key not in self.objects:
            raise urllib.error.HTTPError(url, 404, "no such object", {}, io.BytesIO())
        if key in self.serve:
            return Bytes(*self.serve[key])
        body = self.objects[key]
        return Bytes(body, "image/png" if key.endswith(".png") else "image/jpeg")


class Harness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env = {"GENAIE_AD_ASSETS_SOURCE": "r2",
                    "GENAIE_AD_ASSETS_BASE_URL": BASE,
                    "GENAIE_AD_ASSETS_DIR": os.path.join(self.dir, "cache"),
                    "GENAIE_FIRESTORE_DIR": os.path.join(self.dir, "fs"),
                    "GCP_SERVICE_ACCOUNT_B64": creds_b64()}
        self.clock = [1_800_000_000.0]
        self.err = io.StringIO()
        self.world = World()
        self.signed = 0

    # --- fixtures -------------------------------------------------------
    def signer(self, header, claims, key):
        self.signed += 1
        return "jwt-%d" % self.signed

    def store(self, *entries):
        self.world.firestore.store[aa.COLLECTION] = dict(
            (e.pop("id"), oc.encode_fields(e)) for e in
            [dict(e, id=e.get("id") or "%s-%d" % (e["campaign"],
                                                  (e.get("rank") or 10) // 10))
             for e in entries])

    def hold(self, *bodies):
        for body in bodies:
            kind = ".png" if body.startswith(b"\x89PNG") else ".jpg"
            for campaign in ("cv", "borehole-drilling"):
                self.world.objects["ads/%s/%s%s" % (campaign, digest(body), kind)] = body

    def baked(self, campaign, *names, write=None):
        """Paths shaped like the real baked assets, optionally with contents."""
        directory = os.path.join(self.dir, "cron", "%s-promo" % campaign, "assets")
        os.makedirs(directory, exist_ok=True)
        out = []
        for index_, name in enumerate(names):
            path = os.path.join(directory, name)
            if write is not None:
                with open(path, "wb") as handle:
                    handle.write(write[index_] if isinstance(write, (list, tuple))
                                 else write)
            out.append(path)
        return out

    def inject(self):
        return {"opener": self.world, "signer": self.signer,
                "now": lambda: self.clock[0], "sleep": lambda _s: None}

    def resolve(self, promo, image):
        return aa.resolve(promo, image, self.env, err=self.err, **self.inject())

    def index(self):
        return aa.index(self.env, **self.inject())

    def logged(self):
        return self.err.getvalue()


# ------------------------------------------------------------------- modes ----
class ModeTest(Harness):
    def test_mode_defaults_to_repo(self):
        self.assertEqual(aa.mode({}), (aa.REPO, None))
        self.assertEqual(aa.mode({"GENAIE_AD_ASSETS_SOURCE": ""}), (aa.REPO, None))

    def test_an_unknown_value_reads_as_repo_and_warns(self):
        current, warning = aa.mode({"GENAIE_AD_ASSETS_SOURCE": "R2 "})
        self.assertEqual(current, aa.R2)       # trimmed and lowercased
        current, warning = aa.mode({"GENAIE_AD_ASSETS_SOURCE": "firestore"})
        self.assertEqual(current, aa.REPO)
        self.assertIn("is not one of", warning)

    def test_repo_mode_returns_the_image_unchanged_and_opens_no_socket(self):
        self.env["GENAIE_AD_ASSETS_SOURCE"] = "repo"
        paths = self.baked("cv", "cv-ad.png", write=PNG)
        self.store(entry("cv", "cv-ad.png", PNG2))
        self.hold(PNG2)
        self.assertEqual(self.resolve("cv-promo", paths), paths)
        self.assertEqual(self.resolve("cv-promo", paths[0]), paths[0])
        self.assertEqual(self.world.gets, [])
        self.assertEqual(self.world.firestore.requests, [])

    def test_shadow_mode_returns_the_image_unchanged(self):
        self.env["GENAIE_AD_ASSETS_SOURCE"] = "shadow"
        paths = self.baked("cv", "cv-ad.png", write=PNG)
        self.assertEqual(self.resolve("cv-promo", paths), paths)
        self.assertEqual(self.world.gets, [])


# ------------------------------------------------------------------- index ----
class IndexTest(Harness):
    def test_entries_come_back_in_rank_order_enabled_only(self):
        self.store(entry("cv", "cv-ad.png", PNG, rank=10),
                   entry("cv", "cv-ad-2.png", PNG2, rank=20),
                   entry("cv", "cv-ad-3.jpg", JPG, rank=30, enabled=False))
        found = self.index()
        self.assertEqual(found["source"], "firestore")
        self.assertEqual([e["filename"] for e in found["campaigns"]["cv"]],
                         ["cv-ad.png", "cv-ad-2.png"])

    def test_rank_decides_the_order_even_when_it_contradicts_the_document_ids(self):
        # The regression that passed every earlier test: rows arrive with
        # Firestore's typed values, so ranking before decoding sorts by id and
        # ignores the operator's rotation order entirely. Ids ascending, ranks
        # descending -- only a real decode tells them apart.
        self.store(entry("cv", "cv-ad.png", PNG, rank=30, id="cv-1"),
                   entry("cv", "cv-ad-2.png", PNG2, rank=20, id="cv-2"),
                   entry("cv", "cv-ad-3.jpg", JPG, rank=10, id="cv-3"))
        self.assertEqual(
            [e["filename"] for e in self.index()["campaigns"]["cv"]],
            ["cv-ad-3.jpg", "cv-ad-2.png", "cv-ad.png"])

    def test_ten_images_do_not_sort_cv_10_before_cv_2(self):
        bodies = [b"\x89PNG\r\n\x1a\n" + bytes([i]) * 8 for i in range(10)]
        self.store(*[entry("cv", "cv-%d.png" % (i + 1), body, rank=(i + 1) * 10,
                           id="cv-%d" % (i + 1))
                     for i, body in enumerate(bodies)])
        self.assertEqual([e["filename"] for e in self.index()["campaigns"]["cv"]],
                         ["cv-%d.png" % (i + 1) for i in range(10)])

    def test_an_undecodable_document_quarantines_only_itself(self):
        good = entry("cv", "cv-ad.png", PNG, rank=10)
        self.store(good, entry("cv", "cv-ad-2.png", PNG2, rank=20, sha256="nope"))
        found = self.index()
        self.assertEqual([e["filename"] for e in found["campaigns"]["cv"]],
                         ["cv-ad.png"])
        self.assertEqual(len(found["quarantined"]), 1)

    def test_a_key_that_fails_the_pattern_is_refused(self):
        for key in ("//elsewhere.example/x.png", "../../etc/passwd",
                    "https://elsewhere.example/x.png", "ads/cv/x.png",
                    "ads/cv/%s.gif" % ("a" * 64)):
            with self.assertRaises(ValueError):
                aa._entry("cv-1", entry("cv", "cv-ad.png", PNG, key=key))

    def test_a_key_whose_digest_disagrees_with_sha256_is_refused(self):
        bad = entry("cv", "cv-ad.png", PNG)
        bad["key"] = "ads/cv/%s.png" % ("b" * 64)
        with self.assertRaisesRegex(ValueError, "names digest"):
            aa._entry("cv-1", bad)

    def test_an_oversized_or_mislabelled_entry_is_refused(self):
        for broken in ({"bytes": aa.MAX_BYTES + 1}, {"bytes": 0}, {"bytes": True},
                       {"content_type": "image/gif"}, {"enabled": "yes"},
                       {"rank": "first"},
                       {"filename": "cv-ad.svg"}, {"campaign": "CV"},
                       {"campaign": 7}, {"sha256": None}):
            with self.assertRaises(ValueError):
                aa._entry("cv-1", dict(entry("cv", "cv-ad.png", PNG), **broken))

    def test_a_filename_with_a_path_in_it_is_reduced_to_its_basename(self):
        # Stripped rather than refused: the digest and the campaign already come
        # from fields of their own, and what matters is that nothing reaches
        # cache_path that could climb out of the cache directory.
        item = aa._entry("cv-1", dict(entry("cv", "cv-ad.png", PNG),
                                      filename="../../etc/cv-ad.png"))
        self.assertEqual(item["filename"], "cv-ad.png")
        self.assertEqual(os.path.dirname(aa.cache_path(item, self.env)),
                         os.path.join(aa.cache_dir(self.env), "cv"))

    def test_a_missing_required_field_is_refused(self):
        for name in ("campaign", "filename", "key", "sha256", "bytes", "content_type"):
            partial = entry("cv", "cv-ad.png", PNG)
            del partial[name]
            with self.assertRaises(ValueError):
                aa._entry("cv-1", partial)

    def test_an_entry_whose_extension_contradicts_its_type_is_refused(self):
        bad = entry("cv", "cv-ad.png", PNG)
        bad["content_type"] = "image/jpeg"
        with self.assertRaisesRegex(ValueError, "content_type"):
            aa._entry("cv-1", bad)

    def test_a_non_https_base_url_is_refused(self):
        for raw in ("http://ads.example.test", "ftp://x", "ads.example.test"):
            with self.assertRaises(aa.AssetsUnavailable):
                aa.base_url({"GENAIE_AD_ASSETS_BASE_URL": raw})
        self.assertIsNone(aa.base_url({}))
        self.assertEqual(aa.base_url({"GENAIE_AD_ASSETS_BASE_URL": BASE + "/"}), BASE)

    def test_firestore_down_serves_the_snapshot(self):
        self.store(entry("cv", "cv-ad.png", PNG))
        self.index()                             # writes the snapshot
        self.world.firestore.down = True
        found = self.index()
        self.assertEqual(found["source"], "snapshot")
        self.assertEqual([e["filename"] for e in found["campaigns"]["cv"]],
                         ["cv-ad.png"])

    def test_a_snapshot_past_the_age_bound_is_refused(self):
        self.store(entry("cv", "cv-ad.png", PNG))
        self.index()
        self.world.firestore.down = True
        self.clock[0] += aa.INDEX_MAX_AGE_SECONDS + 60
        with self.assertRaisesRegex(aa.AssetsUnavailable, "past the"):
            self.index()

    def test_no_snapshot_and_no_firestore_is_unavailable(self):
        self.world.firestore.down = True
        with self.assertRaisesRegex(aa.AssetsUnavailable, "no snapshot"):
            self.index()

    def test_no_service_account_is_unavailable_not_a_crash(self):
        self.env.pop("GCP_SERVICE_ACCOUNT_B64")
        with self.assertRaises(aa.AssetsUnavailable):
            self.index()

    def test_the_snapshot_keeps_disabled_entries_but_never_serves_them(self):
        self.store(entry("cv", "cv-ad.png", PNG, rank=10),
                   entry("cv", "cv-ad-2.png", PNG2, rank=20, enabled=False))
        self.index()
        with open(aa.index_path(self.env), encoding="utf-8") as handle:
            written = json.load(handle)
        self.assertEqual(len(written["campaigns"]["cv"]), 2)
        self.world.firestore.down = True
        self.assertEqual([e["filename"] for e in self.index()["campaigns"]["cv"]],
                         ["cv-ad.png"])


# ------------------------------------------------------------------- store ----
class CollectionTest(unittest.TestCase):
    def test_the_index_is_not_a_collection_the_seed_prunes(self):
        # seed-firestore.py --write --prune deletes every document in
        # operator_config.COLLECTIONS that membership.json does not declare,
        # and membership.json declares no images. Listing the index there
        # would hand all of it to the next re-seed, which is why it is its
        # own collection -- the same reason operator_writer keeps the audit
        # trail out of COLLECTIONS.
        self.assertNotIn(aa.COLLECTION, oc.COLLECTIONS)


# ------------------------------------------------------------------- bytes ----
class CacheTest(Harness):
    def test_cache_path_carries_the_filename_after_the_digest(self):
        path = aa.cache_path(entry("cv", "cv-ad.png", PNG), self.env)
        self.assertEqual(os.path.basename(path), "%s-cv-ad.png" % digest(PNG))
        self.assertEqual(os.path.basename(os.path.dirname(path)), "cv")

    def test_the_cache_lives_under_the_state_volumes_media_tree(self):
        # openclaw only reads outbound local media from an allowlist of roots.
        # <state>/media is on it; <state>/runtime is not.
        where = aa.cache_dir({"OPENCLAW_STATE_DIR": "/home/joseph/.openclaw"})
        self.assertEqual(where, "/home/joseph/.openclaw/media/ad-assets")

    def test_an_asset_is_downloaded_verified_and_cached(self):
        self.hold(PNG)
        item = entry("cv", "cv-ad.png", PNG)
        path = aa.ensure(item, self.env, opener=self.world, sleep=lambda _s: None)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), PNG)
        self.assertEqual(len(self.world.gets), 1)
        # Second call is served from the cache.
        self.assertEqual(aa.ensure(item, self.env, opener=self.world), path)
        self.assertEqual(len(self.world.gets), 1)

    def test_a_cached_file_whose_digest_does_not_match_is_fetched_again(self):
        self.hold(PNG)
        item = entry("cv", "cv-ad.png", PNG)
        path = aa.ensure(item, self.env, opener=self.world, sleep=lambda _s: None)
        with open(path, "wb") as handle:
            handle.write(b"tampered")
        self.assertEqual(aa.ensure(item, self.env, opener=self.world,
                                   sleep=lambda _s: None), path)
        self.assertEqual(len(self.world.gets), 2)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), PNG)

    def test_a_body_over_the_image_limit_is_refused(self):
        big = b"\x89PNG\r\n\x1a\n" + b"x" * aa.MAX_BYTES
        item = entry("cv", "cv-ad.png", big)
        item["bytes"] = aa.MAX_BYTES          # the index lies about the size
        self.world.objects[item["key"]] = big
        with self.assertRaisesRegex(aa.AssetUnusable, "larger than"):
            aa.ensure(item, self.env, opener=self.world, sleep=lambda _s: None)

    def test_a_wrong_size_or_digest_or_content_type_is_refused(self):
        item = entry("cv", "cv-ad.png", PNG)
        self.world.objects[item["key"]] = PNG
        self.world.serve[item["key"]] = (PNG, "text/html")
        with self.assertRaisesRegex(aa.AssetUnusable, "served as text/html"):
            aa.ensure(item, self.env, opener=self.world, sleep=lambda _s: None)
        self.world.serve[item["key"]] = (PNG2, "image/png")
        with self.assertRaisesRegex(aa.AssetUnusable, "bytes, the index says"):
            aa.ensure(item, self.env, opener=self.world, sleep=lambda _s: None)
        same_size = PNG[:-1] + b"!"
        self.assertEqual(len(same_size), len(PNG))
        self.world.serve[item["key"]] = (same_size, "image/png")
        with self.assertRaisesRegex(aa.AssetUnusable, "hashes to"):
            aa.ensure(item, self.env, opener=self.world, sleep=lambda _s: None)

    def test_the_fetch_names_itself_rather_than_sending_urllibs_default(self):
        # r2.dev answers 403 to "Python-urllib/3.x" and 200 to anything else, so
        # an unset User-Agent means every asset fetch fails and every campaign
        # quietly posts its baked image instead.
        self.hold(PNG)
        aa.ensure(entry("cv", "cv-ad.png", PNG), self.env, opener=self.world,
                  sleep=lambda _s: None)
        self.assertEqual(self.world.agents, [aa.USER_AGENT])
        for agent in self.world.agents:
            self.assertNotIn("urllib", (agent or "").lower())
            self.assertTrue(agent)

    def test_a_missing_object_retries_once_then_gives_up(self):
        item = entry("cv", "cv-ad.png", PNG)
        with self.assertRaisesRegex(aa.AssetUnusable, "404"):
            aa.ensure(item, self.env, opener=self.world, sleep=lambda _s: None)
        self.assertEqual(len(self.world.gets), 1)   # 404 is not retryable

    def test_no_base_url_names_the_variable_rather_than_crashing(self):
        self.env.pop("GENAIE_AD_ASSETS_BASE_URL")
        with self.assertRaisesRegex(aa.AssetUnusable, aa.BASE_VAR):
            aa.ensure(entry("cv", "cv-ad.png", PNG), self.env, opener=self.world)

    def test_prune_removes_only_unreferenced_cache_files(self):
        self.hold(PNG, PNG2)
        keep = entry("cv", "cv-ad.png", PNG)
        drop = entry("cv", "old.png", PNG2)
        for item in (keep, drop):
            aa.ensure(item, self.env, opener=self.world, sleep=lambda _s: None)
        removed = aa.prune({"campaigns": {"cv": [keep]}}, self.env, log=lambda _m: None)
        self.assertEqual(removed, 1)
        self.assertTrue(os.path.exists(aa.cache_path(keep, self.env)))
        self.assertFalse(os.path.exists(aa.cache_path(drop, self.env)))


# ----------------------------------------------------------------- resolve ----
class ResolveTest(Harness):
    def test_a_single_baked_string_becomes_the_whole_rotation(self):
        # The point of the move: adding a second picture is an index edit, not a
        # change to the campaign's post script.
        path, = self.baked("genaie", "genaie-services.png", write=PNG)
        self.store(entry("genaie", "genaie-services.png", PNG, rank=10),
                   entry("genaie", "genaie-2.png", PNG2, rank=20))
        self.world.objects.update(
            ("ads/genaie/%s.png" % digest(b), b) for b in (PNG, PNG2))
        got = self.resolve("genaie-promo", path)
        self.assertEqual([os.path.basename(p).split("-", 1)[1] for p in got],
                         ["genaie-services.png", "genaie-2.png"])

    def test_a_baked_list_is_replaced_in_rank_order(self):
        paths = self.baked("cv", "cv-ad.png", "cv-ad-2.png", write=[PNG, PNG2])
        self.store(entry("cv", "cv-ad.png", PNG, rank=10),
                   entry("cv", "cv-ad-2.png", PNG2, rank=20))
        self.hold(PNG, PNG2)
        got = self.resolve("cv-promo", paths)
        self.assertEqual(len(got), 2)
        for path in got:
            self.assertTrue(path.startswith(aa.cache_dir(self.env)))

    def test_a_path_outside_the_campaigns_assets_dir_passes_through(self):
        # An operator's *_IMAGE override, or a test's temp file. Every post
        # script has such an override, and it must keep working in r2 mode.
        override = os.path.join(self.dir, "hand-made.png")
        with open(override, "wb") as handle:
            handle.write(PNG)
        self.store(entry("cv", "cv-ad.png", PNG2))
        self.hold(PNG2)
        self.assertEqual(self.resolve("cv-promo", override), override)
        self.assertEqual(self.resolve("cv-promo", [override]), [override])
        self.assertEqual(self.world.gets, [])

    def test_another_campaigns_asset_is_not_treated_as_this_ones(self):
        other, = self.baked("borehole-drilling", "borehole-1.jpg", write=JPG)
        self.assertFalse(aa.is_baked(other, "cv-promo"))
        self.assertTrue(aa.is_baked(other, "borehole-drilling-promo"))
        self.assertTrue(aa.is_baked(other, "borehole-drilling"))

    def test_an_empty_index_for_a_campaign_keeps_the_baked_image(self):
        paths = self.baked("cv", "cv-ad.png", write=PNG)
        self.store(entry("borehole-drilling", "borehole-1.jpg", JPG))
        self.assertEqual(self.resolve("cv-promo", paths), paths)
        self.assertIn("no enabled assets indexed for cv", self.logged())

    def test_one_unreachable_object_leaves_the_others_serving(self):
        paths = self.baked("cv", "cv-ad.png", "cv-ad-2.png", write=[PNG, PNG2])
        self.store(entry("cv", "cv-ad.png", PNG, rank=10),
                   entry("cv", "cv-ad-2.png", PNG2, rank=20))
        self.hold(PNG, PNG2)
        self.world.missing.add("ads/cv/%s.png" % digest(PNG))
        got = self.resolve("cv-promo", paths)
        self.assertEqual(len(got), 1)
        self.assertIn("dropping cv-ad.png from the rotation", self.logged())

    def test_every_object_unreachable_falls_back_to_the_baked_image(self):
        paths = self.baked("cv", "cv-ad.png", write=PNG)
        self.store(entry("cv", "cv-ad.png", PNG))
        got = self.resolve("cv-promo", paths)
        self.assertEqual(got, paths)
        self.assertIn("using the baked image", self.logged())

    def test_with_nothing_baked_the_result_names_the_missing_asset(self):
        # What the engine prints as "promo image missing: <path>" once the files
        # are gone from the repo. Never None, never an empty list.
        paths = self.baked("cv", "cv-ad.png")        # declared, not written
        item = entry("cv", "cv-ad.png", PNG)
        self.store(dict(item))
        got = self.resolve("cv-promo", paths)
        self.assertIsInstance(got, str)
        self.assertIn(digest(PNG)[:12], got)
        self.assertIn("cv-ad.png", got)

    def test_resolve_never_returns_none_or_an_empty_list(self):
        paths = self.baked("cv", "cv-ad.png")
        for store in ([], [entry("cv", "cv-ad.png", PNG)]):
            self.store(*[dict(s) for s in store])
            for image in (paths, paths[0]):
                got = self.resolve("cv-promo", image)
                self.assertTrue(got, "resolve returned %r" % (got,))

    def test_an_unreadable_index_falls_back_to_the_baked_image(self):
        paths = self.baked("cv", "cv-ad.png", write=PNG)
        self.world.firestore.down = True
        self.assertEqual(self.resolve("cv-promo", paths), paths)

    def test_a_bug_in_this_module_cannot_stop_a_run_posting(self):
        paths = self.baked("cv", "cv-ad.png", write=PNG)
        broken = dict(self.inject(), opener=None)
        original = aa.index
        aa.index = lambda *a, **k: 1 / 0
        self.addCleanup(setattr, aa, "index", original)
        self.assertEqual(aa.resolve("cv-promo", paths, self.env, err=self.err,
                                    **broken), paths)
        self.assertIn("could not resolve assets", self.logged())


class VariantImageTest(Harness):
    def test_variant_images_are_mapped_by_filename(self):
        one, two = self.baked("cv", "cv-ad.png", "cv-ad-2.png", write=[PNG, PNG2])
        self.store(entry("cv", "cv-ad.png", PNG, rank=10),
                   entry("cv", "cv-ad-2.png", PNG2, rank=20))
        self.hold(PNG, PNG2)
        got = self.resolve("cv-promo", {0: two})
        self.assertNotEqual(got[0], two)
        self.assertTrue(got[0].endswith("-cv-ad-2.png"))

    def test_an_unindexed_variant_keeps_its_baked_image(self):
        one, = self.baked("cv", "cv-ad.png", write=PNG)
        self.store(entry("cv", "cv-ad-2.png", PNG2))
        self.hold(PNG2)
        self.assertEqual(self.resolve("cv-promo", {0: one}), {0: one})
        self.assertIn("no asset indexed under that name", self.logged())

    def test_an_empty_mapping_is_left_alone(self):
        self.assertEqual(self.resolve("cv-promo", {}), {})


# ------------------------------------------------------------------ shadow ----
class ShadowTest(Harness):
    def setUp(self):
        super().setUp()
        self.env["GENAIE_AD_ASSETS_SOURCE"] = "shadow"

    def check(self, promo, image):
        return aa.check(promo, image, self.env, **self.inject())

    def test_matching_index_and_baked_images_report_exact_and_warm_the_cache(self):
        paths = self.baked("cv", "cv-ad.png", write=PNG)
        self.store(entry("cv", "cv-ad.png", PNG))
        self.hold(PNG)
        result = self.check("cv-promo", paths)
        self.assertEqual(result["status"], "exact")
        self.assertEqual(result["cached"], ["cv-ad.png"])
        self.assertTrue(os.path.exists(
            aa.cache_path(entry("cv", "cv-ad.png", PNG), self.env)))

    def test_a_changed_picture_reports_drift(self):
        paths = self.baked("cv", "cv-ad.png", write=PNG)
        self.store(entry("cv", "cv-ad.png", PNG2))
        self.hold(PNG2)
        result = self.check("cv-promo", paths)
        self.assertEqual(result["status"], "drift")
        self.assertIn("cv-ad.png differs from the indexed copy", result["problems"])

    def test_an_unindexed_or_unbaked_picture_reports_drift(self):
        paths = self.baked("cv", "cv-ad.png", "extra.png", write=[PNG, PNG2])
        self.store(entry("cv", "cv-ad.png", PNG), entry("cv", "ghost.png", JPG, rank=20))
        self.hold(PNG, JPG)
        result = self.check("cv-promo", paths)
        self.assertIn("extra.png is not in the index", result["problems"])
        self.assertIn("ghost.png is indexed but not baked into this image",
                      result["problems"])

    def test_an_unreadable_index_reports_unavailable_not_an_exception(self):
        paths = self.baked("cv", "cv-ad.png", write=PNG)
        self.world.firestore.down = True
        result = self.check("cv-promo", paths)
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNotNone(result["degraded"])

    def test_the_result_is_written_where_the_console_reads_it(self):
        paths = self.baked("cv", "cv-ad.png", write=PNG)
        self.store(entry("cv", "cv-ad.png", PNG))
        self.hold(PNG)
        self.check("cv-promo", paths)
        saved = aa.read_result(self.env)
        self.assertEqual(saved["campaign"], "cv")
        self.assertEqual(saved["status"], "exact")

    def test_a_hung_check_does_not_delay_the_run(self):
        gate = threading.Event()
        self.world.hang = gate
        self.addCleanup(gate.set)
        paths = self.baked("cv", "cv-ad.png", write=PNG)
        self.store(entry("cv", "cv-ad.png", PNG))
        self.hold(PNG)
        handle = aa.start("cv-promo", paths, self.env, err=self.err, **self.inject())
        self.assertIsNotNone(handle)
        self.assertIsNone(aa.finish(handle, timeout=0.05, err=self.err))
        self.assertIn("not waiting for it", self.logged())

    def test_start_does_nothing_outside_shadow_mode(self):
        for value in ("repo", "r2", ""):
            self.env["GENAIE_AD_ASSETS_SOURCE"] = value
            self.assertIsNone(aa.start("cv-promo", [], self.env, err=self.err))

    def test_alongside_passes_the_runs_value_and_exceptions_through(self):
        self.env["GENAIE_AD_ASSETS_SOURCE"] = "repo"

        @aa.alongside
        def ran(*, image=None, promo_name=None, targets_file=None):
            return "the run's own answer"

        @aa.alongside
        def exploded(*, image=None, promo_name=None, targets_file=None):
            raise RuntimeError("the run's own failure")

        self.assertEqual(ran(image=[], promo_name="cv-promo"), "the run's own answer")
        with self.assertRaisesRegex(RuntimeError, "the run's own failure"):
            exploded(image=[], promo_name="cv-promo")

    def test_alongside_derives_the_campaign_from_the_targets_file(self):
        seen = {}
        original = aa.start
        def record(promo, image, *a, **k):
            seen["promo"] = promo
            return None

        aa.start = record
        self.addCleanup(setattr, aa, "start", original)

        @aa.alongside
        def ran(*, image=None, targets_file=None, promo_name=None):
            return 0

        ran(image=[], targets_file="/x/workspace/cron/cv-promo/targets.json")
        self.assertEqual(seen["promo"], "cv-promo")


if __name__ == "__main__":
    unittest.main()
