"""What a campaign run does once R2 holds the ad images.

ad_assets is tested on its own (workspace/lib/test_ad_assets.py); this covers
the three things only the engine can get wrong, through the real
``promo_broadcast.broadcast`` and the real ``ad_assets.resolve``, wired the way
production wires them -- through ``os.environ`` and ``urllib.request.urlopen``,
with no injection seam:

* an r2-mode run attaches the cached file to every group it posts to,
* no usable asset and nothing baked means NO SEND and a non-zero exit, so the
  cron's failure alert fires rather than the day passing silently,
* the path handed to the sender is stable run to run, because the RPC transport
  hashes it into the idempotency key.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes it.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT / "workspace/lib"), str(ROOT / "deploy")):
    if path not in sys.path:
        sys.path.insert(0, path)

import ad_assets  # noqa: E402
import operator_config  # noqa: E402
import promo_broadcast as pb  # noqa: E402
import promo_membership  # noqa: E402
from test_ad_assets import BASE, PNG, PNG2, World, entry  # noqa: E402
from test_operator_config import creds_b64  # noqa: E402

CRON = str(ROOT / "workspace/cron")
PROMO = "genaie"
DAY = date(2026, 11, 6)


def pin(day):
    class _Date(date):
        @classmethod
        def today(cls):
            return day
    return _Date


class Harness(unittest.TestCase):
    """A real run with a fake sender, a fake Firestore, a fake R2 and no delays."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state = os.path.join(self.dir, "state.json")
        membership = json.loads(
            (ROOT / "workspace/cron/membership.json").read_text(encoding="utf-8"))
        self.targets = promo_membership.targets_path(membership, PROMO, CRON)

        # Baked assets, shaped exactly like the real ones so is_baked matches.
        self.assets = os.path.join(self.dir, "cron", "%s-promo" % PROMO, "assets")
        os.makedirs(self.assets)
        self.baked = os.path.join(self.assets, "genaie-services.png")

        self.world = World()
        self.entries = []
        self.clock = [1_800_000_000.0]

        # Production reads the environment and calls urllib directly; so does
        # this, because the engine's one call site injects nothing.
        for name, value in {
                "GENAIE_AD_ASSETS_SOURCE": "r2",
                "GENAIE_AD_ASSETS_BASE_URL": BASE,
                "GENAIE_AD_ASSETS_DIR": os.path.join(self.dir, "cache"),
                "GENAIE_FIRESTORE_DIR": os.path.join(self.dir, "fs"),
                "GCP_SERVICE_ACCOUNT_B64": creds_b64()}.items():
            self.patch_env(name, value)
        for name in ("GENAIE_CONFIG_SOURCE", "GENAIE_CAMPAIGN_TRANSPORT"):
            self.patch_env(name, None)

        self.patch(urllib.request, "urlopen", self.world)
        self.patch(operator_config, "node_signer",
                   lambda header, claims, key: "jwt")
        self.patch(operator_config, "time", _Clock(self.clock))
        self.patch(ad_assets, "time", _Clock(self.clock, monotonic=True))

        self.sent = []
        self.patch(pb, "send_ad", lambda target, text, image, account=None: (
            self.sent.append((target, image)) or (True, '{"messageId":"m"}')))
        self.patch(pb, "date", pin(DAY))

    # --- patching -------------------------------------------------------
    def patch(self, target, name, value):
        saved = getattr(target, name)
        setattr(target, name, value)
        self.addCleanup(setattr, target, name, saved)

    def patch_env(self, name, value):
        saved = os.environ.get(name)

        def restore():
            if saved is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = saved
        self.addCleanup(restore)
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    # --- fixtures -------------------------------------------------------
    def bake(self, body=PNG):
        with open(self.baked, "wb") as handle:
            handle.write(body)

    def index(self, *bodies):
        store = {}
        for position, body in enumerate(bodies, start=1):
            item = entry(PROMO, "genaie-services.png" if position == 1
                         else "genaie-%d.png" % position, body, rank=position * 10)
            self.world.objects[item["key"]] = body
            store["%s-%d" % (PROMO, position)] = operator_config.encode_fields(item)
            self.entries.append(item)
        self.world.firestore.store[ad_assets.COLLECTION] = store

    def run_broadcast(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = pb.broadcast(text="Ad {bot_number}", image=self.baked,
                              targets_file=self.targets, state_file=self.state,
                              promo_name=PROMO + "-promo", send_delay=0, argv=[],
                              start_jitter=0, caps=None, cron_timeout=None,
                              account_status=lambda a: {x: True for x in a})
        return rc, out.getvalue(), err.getvalue()


class _Clock(object):
    """``time`` with a clock the test moves, for modules that call it directly."""

    def __init__(self, box, monotonic=False):
        self._box = box
        self._monotonic = monotonic

    def time(self):
        return self._box[0]

    def monotonic(self):
        return self._box[0] if self._monotonic else self._box[0]

    def sleep(self, _seconds):
        return None


class SendTest(Harness):
    def test_every_group_is_sent_the_cached_file(self):
        self.bake()
        self.index(PNG)
        rc, _out, err = self.run_broadcast()
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.sent)
        expected = ad_assets.cache_path(self.entries[0])
        for target, image in self.sent:
            self.assertEqual(image, expected, target)
        with open(expected, "rb") as handle:
            self.assertEqual(handle.read(), PNG)

    def test_the_cached_file_is_sent_even_when_the_baked_one_differs(self):
        # The whole point: the operator swapped the picture without a deploy.
        self.bake(PNG)
        self.index(PNG2)
        rc, _out, err = self.run_broadcast()
        self.assertEqual(rc, 0, err)
        self.assertNotEqual(self.sent[0][1], self.baked)
        with open(self.sent[0][1], "rb") as handle:
            self.assertEqual(handle.read(), PNG2)

    def test_the_path_is_stable_across_two_runs(self):
        # gateway_campaign hashes the image string into the idempotency key, so
        # an unstable path would make every run look like a new message.
        self.bake()
        self.index(PNG)
        self.run_broadcast()
        first = [image for _target, image in self.sent]
        self.sent.clear()
        os.unlink(self.state)
        self.run_broadcast()
        self.assertEqual(first, [image for _target, image in self.sent])


class FailClosedTest(Harness):
    def test_no_usable_asset_and_nothing_baked_sends_nothing_and_exits_one(self):
        self.index(PNG)
        self.world.missing.add(self.entries[0]["key"])
        # No self.bake(): this is the world after the images leave the repo.
        rc, _out, err = self.run_broadcast()
        self.assertEqual(rc, 1)
        self.assertEqual(self.sent, [])
        self.assertIn("promo image missing", err)
        # The alert has to name the picture, not say "None".
        self.assertNotIn("promo image missing: None", err)
        self.assertIn("genaie-services.png", err)

    def test_an_unreachable_object_falls_back_to_the_baked_image(self):
        self.bake()
        self.index(PNG2)
        self.world.missing.add(self.entries[0]["key"])
        rc, _out, err = self.run_broadcast()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.sent[0][1], self.baked)
        self.assertIn("using the baked image", err)

    def test_an_unreadable_index_falls_back_to_the_baked_image(self):
        self.bake()
        self.index(PNG)
        self.world.firestore.down = True
        rc, _out, err = self.run_broadcast()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.sent[0][1], self.baked)

    def test_repo_mode_sends_the_baked_image_and_touches_no_network(self):
        os.environ["GENAIE_AD_ASSETS_SOURCE"] = "repo"
        self.bake()
        self.index(PNG2)
        rc, _out, err = self.run_broadcast()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.sent[0][1], self.baked)
        self.assertEqual(self.world.gets, [])


if __name__ == "__main__":
    unittest.main()
