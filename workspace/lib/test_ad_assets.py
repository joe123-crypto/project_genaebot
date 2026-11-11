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


# --------------------------------------------------------------- catalogue ----
class CatalogueTest(Harness):
    """``catalogue`` is what the operator console reads: it describes the index
    rather than fetching it, must never raise, and must not put a Firestore read
    behind every page load."""

    def catalogue(self, **kwargs):
        inject = self.inject()
        inject.pop("sleep")
        return aa.catalogue(self.env, sleep=lambda _s: None, **inject, **kwargs)

    def test_every_entry_carries_the_address_its_bytes_are_served_from(self):
        self.store(entry("cv", "cv-ad.png", PNG, rank=10),
                   entry("cv", "cv-ad-2.png", PNG2, rank=20))
        found = self.catalogue()
        self.assertEqual(found["source"], "firestore")
        self.assertIsNone(found["note"])
        self.assertEqual([e["url"] for e in found["campaigns"]["cv"]],
                         ["%s/ads/cv/%s.png" % (BASE, digest(PNG)),
                          "%s/ads/cv/%s.png" % (BASE, digest(PNG2))])

    def test_public_url_is_the_url_ensure_would_fetch(self):
        one = entry("cv", "cv-ad.png", PNG)
        self.assertEqual(aa.public_url(one, self.env), "%s/%s" % (BASE, one["key"]))
        self.assertIsNone(aa.public_url(one, {}))

    def test_a_fresh_snapshot_is_reused_instead_of_reading_firestore(self):
        self.store(entry("cv", "cv-ad.png", PNG))
        self.catalogue()
        before = len(self.world.firestore.requests)
        self.clock[0] += aa.CATALOGUE_MAX_AGE_SECONDS - 1
        found = self.catalogue()
        self.assertEqual(found["source"], "snapshot")
        self.assertIsNone(found["note"])          # reuse is normal, not degraded
        self.assertEqual([e["filename"] for e in found["campaigns"]["cv"]],
                         ["cv-ad.png"])
        self.assertEqual(len(self.world.firestore.requests), before)

    def test_a_stale_snapshot_falls_through_to_a_live_read(self):
        self.store(entry("cv", "cv-ad.png", PNG))
        self.catalogue()
        self.clock[0] += aa.CATALOGUE_MAX_AGE_SECONDS + 1
        self.store(entry("cv", "cv-ad.png", PNG), entry("cv", "cv-ad-2.png", PNG2,
                                                        rank=20))
        found = self.catalogue()
        self.assertEqual(found["source"], "firestore")
        self.assertEqual(len(found["campaigns"]["cv"]), 2)

    def test_an_unreadable_index_is_a_note_rather_than_an_exception(self):
        # The console's caller is a page where the pictures are one card. An
        # index it cannot read must cost the pictures and nothing else, so the
        # failure arrives as a note beside an empty campaigns map rather than as
        # a screen that will not load.
        self.env.pop("GCP_SERVICE_ACCOUNT_B64")
        found = self.catalogue()
        self.assertIsNone(found["source"])
        self.assertEqual(found["campaigns"], {})
        self.assertIn("no service account", found["note"])

    def test_firestore_down_falls_back_to_the_snapshot_and_says_so(self):
        self.store(entry("cv", "cv-ad.png", PNG))
        self.catalogue()
        self.clock[0] += aa.CATALOGUE_MAX_AGE_SECONDS + 1
        self.world.firestore.down = True
        found = self.catalogue()
        self.assertEqual(found["source"], "snapshot")
        self.assertEqual(len(found["campaigns"]["cv"]), 1)
        self.assertTrue(found["note"])

    def test_no_base_url_names_the_pictures_without_an_address(self):
        self.env.pop("GENAIE_AD_ASSETS_BASE_URL")
        self.store(entry("cv", "cv-ad.png", PNG))
        found = self.catalogue()
        self.assertEqual([e["filename"] for e in found["campaigns"]["cv"]],
                         ["cv-ad.png"])
        self.assertEqual([e["url"] for e in found["campaigns"]["cv"]], [None])
        self.assertIn("GENAIE_AD_ASSETS_BASE_URL", found["note"])

    def test_a_base_url_that_is_not_https_is_a_note_not_a_crash(self):
        self.env["GENAIE_AD_ASSETS_BASE_URL"] = "http://ads.example.test"
        self.store(entry("cv", "cv-ad.png", PNG))
        found = self.catalogue()
        self.assertEqual([e["url"] for e in found["campaigns"]["cv"]], [None])
        self.assertIn("https://", found["note"])

    # --- not retrying a Firestore that already said no --------------------
    def test_no_index_and_no_snapshot_leaves_nothing_to_show(self):
        # The committed mirror used to answer here, which is what made this
        # survivable in the hours before any run had written a snapshot. It was
        # operator data in the repository and it is gone, so this is now the
        # whole answer: no pictures, and a note saying why.
        self.env.pop("GCP_SERVICE_ACCOUNT_B64")
        found = self.catalogue()
        self.assertEqual(found["campaigns"], {})
        self.assertIsNone(found["source"])
        self.assertIn("no service account", found["note"])

    def test_a_failed_read_is_not_repeated_on_every_call(self):
        self.world.firestore.down = True
        self.catalogue()
        before = len(self.world.firestore.requests)
        self.assertIsNone(self.catalogue()["source"])
        self.assertEqual(len(self.world.firestore.requests), before)

    def test_the_retry_window_expires_so_a_recovered_quota_is_noticed(self):
        self.world.firestore.down = True
        self.catalogue()
        self.world.firestore.down = False
        self.store(entry("cv", "cv-ad.png", PNG2))
        self.clock[0] += aa.RETRY_AFTER_SECONDS + 1
        found = self.catalogue()
        self.assertEqual(found["source"], "firestore")
        self.assertEqual([e["filename"] for e in found["campaigns"]["cv"]], ["cv-ad.png"])

    def test_a_read_that_works_forgets_the_earlier_failure(self):
        self.world.firestore.down = True
        self.catalogue()
        self.assertTrue(os.path.exists(aa.failure_path(self.env)))
        self.world.firestore.down = False
        self.store(entry("cv", "cv-ad.png", PNG2))
        self.clock[0] += aa.RETRY_AFTER_SECONDS + 1
        self.catalogue()
        self.assertFalse(os.path.exists(aa.failure_path(self.env)))

    def test_a_failed_read_over_a_snapshot_still_opens_the_retry_window(self):
        # The branch that always exists in production: a container that has
        # read Firestore once has a snapshot, so a later failure answers out of
        # it instead of raising. That is still a failed read -- without it the
        # backoff would do nothing on any deployed container.
        self.store(entry("cv", "cv-ad.png", PNG))
        self.catalogue()                                    # writes the snapshot
        self.clock[0] += aa.CATALOGUE_MAX_AGE_SECONDS + 1
        self.world.firestore.down = True
        self.assertEqual(self.catalogue()["source"], "snapshot")
        self.assertTrue(os.path.exists(aa.failure_path(self.env)))
        before = len(self.world.firestore.requests)
        found = self.catalogue()
        self.assertEqual(len(self.world.firestore.requests), before)
        # And the window keeps the snapshot: suppressing the read must not cost
        # the fresher answer that read would have fallen back to.
        self.assertEqual(found["source"], "snapshot")
        self.assertEqual([e["filename"] for e in found["campaigns"]["cv"]], ["cv-ad.png"])
        self.assertTrue(found["note"])

    def test_the_window_over_a_snapshot_expires_like_any_other(self):
        self.store(entry("cv", "cv-ad.png", PNG))
        self.catalogue()
        self.clock[0] += aa.CATALOGUE_MAX_AGE_SECONDS + 1
        self.world.firestore.down = True
        self.catalogue()
        self.world.firestore.down = False
        self.store(entry("cv", "cv-ad-2.png", PNG2))
        self.clock[0] += aa.RETRY_AFTER_SECONDS + 1
        found = self.catalogue()
        self.assertEqual(found["source"], "firestore")
        self.assertEqual([e["filename"] for e in found["campaigns"]["cv"]], ["cv-ad-2.png"])
        self.assertFalse(os.path.exists(aa.failure_path(self.env)))

    def test_inside_the_window_a_snapshot_past_the_limit_shows_nothing(self):
        # A snapshot too old to act on, inside the backoff window, used to drop
        # to the committed mirror. There is nothing under it now, and saying so
        # is the right answer: the alternative would be showing pictures from a
        # file nobody had updated in a day.
        self.store(entry("cv", "cv-ad.png", PNG2))
        self.catalogue()
        self.clock[0] += aa.INDEX_MAX_AGE_SECONDS + 1       # snapshot now too old to act on
        self.world.firestore.down = True
        self.assertIsNone(self.catalogue()["source"])
        before = len(self.world.firestore.requests)
        found = self.catalogue()
        self.assertEqual(len(self.world.firestore.requests), before)
        self.assertEqual(found["campaigns"], {})

    def test_it_describes_pictures_without_fetching_a_single_byte(self):
        self.store(entry("cv", "cv-ad.png", PNG))
        self.hold(PNG)
        self.catalogue()
        self.assertEqual(self.world.gets, [])


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
if __name__ == "__main__":
    unittest.main()
