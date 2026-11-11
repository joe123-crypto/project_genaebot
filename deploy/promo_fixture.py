"""One synthetic operator config, and a real campaign run driven by it.

The engine tests used to be handed the live thing: ``deploy/seed-firestore.py``
read the committed ``membership.json`` and the committed cron manifest, and the
tests asserted against whatever the operator happened to have configured that
week. That had two problems even before the files were deleted. It made the
tests a second place real group IDs and phone numbers lived, and it meant a test
could start passing or failing because of an edit to the ad roster.

So the fixture is built here instead, out of synthetic values: numbers in the
unallocated ``+26370…`` range and group IDs of the form
``120363000000000NNN@g.us``. It is deliberately small and deliberately awkward
in the ways that matter -- a weekly group, a paused campaign, two lines, a
group on each, an inactive group that still has campaigns configured -- because
those are the cases the engine gets wrong.

Not a test file itself: it holds no test cases, and ``deploy/ci-check.sh`` runs
it only as a module the test files import.
"""

import copy
import io
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT / "workspace/lib"), str(ROOT / "deploy")):
    if path not in sys.path:
        sys.path.insert(0, path)

import operator_config  # noqa: E402
import operator_writer  # noqa: E402
import promo_broadcast as pb  # noqa: E402
import promo_config  # noqa: E402
from test_operator_config import FakeFirestore, creds_b64  # noqa: E402

# A Friday, so a weekly group configured for Friday is in the run and the same
# fixture on any other day is not.
DAY = date(2026, 11, 6)
LINES = {"default": "+26370000001", "ads": "+26370000002"}


def group(index, promos, *, status="active", weekly=None, account=None,
          paused=None, activity="normal"):
    entry = {"name": None, "status": status, "activity": activity,
             "daily_cap": None, "promos": list(promos)}
    if weekly:
        entry["weekly"] = dict(weekly)
    if account:
        entry["account"] = account
    if paused:
        entry["paused_promos"] = list(paused)
    return "1203630000000%05d@g.us" % index, entry


def schedule(time, *, weekdays=None, timezone="Africa/Harare"):
    return {"time": time, "timezone": timezone,
            "weekdays": list(range(7)) if weekdays is None else list(weekdays)}


def campaign(name, time, *, enabled=True, captions=None, prefix="XX",
             images=1, weekdays=None, job=None, timeout=7200, status="ready"):
    """One data-defined campaign document, as the store holds it."""
    doc = {"kind": "console", "name": name.replace("-", " ").title(),
           "enabled": enabled,
           "captions": captions or ["%s caption one" % name, "%s caption two" % name],
           "tracking": {"label": name, "prefix": prefix},
           "schedule": schedule(time, weekdays=weekdays),
           "timeoutSeconds": timeout, "failureAlertTo": LINES["default"],
           "images": [{"filename": "%s-%d.jpg" % (name, n + 1),
                       "key": "ads/%s/%s.jpg" % (name, "%064x" % (n + 1)),
                       "sha256": "%064x" % (n + 1), "bytes": 100 + n,
                       "contentType": "image/jpeg"} for n in range(images)],
           "scheduleStatus": status}
    if job:
        doc["jobName"] = job
    return doc


def membership():
    """The whole config, in the shape ``promo_config.resolve`` hands back."""
    groups = dict([
        group(1, ["alpha", "beta"]),
        group(2, ["alpha"]),
        group(3, ["alpha", "beta"], weekly={"beta": [4]}),     # Friday only
        group(4, ["alpha"], account="ads"),
        group(5, ["alpha"], status="on-hold"),                 # configured, not sent to
        group(6, ["paused"]),                                  # campaign switched off
        group(7, ["alpha"], paused=["alpha"]),                  # assignment paused
        group(8, ["alpha"], activity="high"),
    ])
    return {
        "version": 1,
        "defaults": {"activity": "normal", "caps_enabled": True,
                     "contact_account": "default",
                     "accounts": dict((name, {"number": number})
                                      for name, number in LINES.items()),
                     "bands": {"normal": [1, 3], "high": [2, 5]}},
        "promos": {
            "alpha": campaign("alpha", "09:07", prefix="AL", images=2,
                              job="alpha-promo"),
            "beta": campaign("beta", "13:21", prefix="BE"),
            "paused": campaign("paused", "15:43", prefix="PA", enabled=False),
        },
        "groups": groups,
    }


def switches(config=None):
    config = config or membership()
    return dict((slug, bool(doc.get("enabled")))
                for slug, doc in config["promos"].items())


def documents(config=None, enabled=None):
    config = config or membership()
    return operator_writer.documents(config, enabled or switches(config))


def wire(docs):
    return dict((collection, dict((doc_id, operator_config.encode_fields(fields))
                                  for doc_id, fields in rows))
                for collection, rows in docs.items())


def pin(day):
    class _Date(date):
        @classmethod
        def today(cls):
            return day
    return _Date


def targets_payload(promo, config=None):
    import promo_membership
    return promo_membership.targets_payload(config or membership(), promo)


class Harness(unittest.TestCase):
    """A real campaign run with a fake sender, a fake Firestore and no delays."""

    PROMO = "alpha"

    def setUp(self):
        self.membership = membership()
        self.switches = switches(self.membership)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.image = os.path.join(self.dir, "ad.png")
        with open(self.image, "wb") as handle:
            handle.write(b"img")
        self.state = os.path.join(self.dir, "state.json")
        self.targets = os.path.join(self.dir, "targets.json")
        self.env = {"GENAIE_FIRESTORE_DIR": os.path.join(self.dir, "fs"),
                    "GCP_SERVICE_ACCOUNT_B64": creds_b64(),
                    "GENAIE_CONFIG_SOURCE": "firestore"}
        self.firestore = FakeFirestore(
            wire(documents(self.membership, self.switches)), page_size=50)
        self.clock = [1_800_000_000.0]
        self.err = io.StringIO()

        self.sent = []
        saved_send, saved_date = pb.send_ad, pb.date
        pb.send_ad = lambda target, text, image, account=None: (
            self.sent.append(target) or (True, '{"messageId":"m"}'))
        pb.date = pin(DAY)

        def restore():
            pb.send_ad, pb.date = saved_send, saved_date
        self.addCleanup(restore)

    def store(self, config=None, enabled=None):
        """Point the fake Firestore at a different config."""
        config = config or self.membership
        self.firestore = FakeFirestore(
            wire(documents(config, enabled or switches(config))), page_size=50)
        return self.firestore

    def resolver(self, env=None):
        env = self.env if env is None else env

        def resolve(promo, **ignored):
            return promo_config.resolve(
                promo, env=env, err=self.err, opener=self.firestore,
                signer=lambda header, claims, key: "jwt",
                now=lambda: self.clock[0], sleep=lambda s: None)
        return resolve

    def run_broadcast(self, env=None, argv=None, state=None):
        """One real campaign run, with a fake sender and no delays.

        ``image`` is a local path, which is what the engine is handed now: the
        caller fetches every indexed picture from R2 before calling in, so this
        does not stand in for an asset resolve that no longer happens here."""
        from contextlib import redirect_stderr, redirect_stdout
        # One buffer for both: what the resolver reports and what the engine
        # reports both land on the run's stderr in production.
        self.err = io.StringIO()
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(self.err):
            rc = pb.broadcast(text="Ad {bot_number}", image=self.image,
                              targets_file=self.targets,
                              state_file=state or self.state,
                              promo_name=self.PROMO + "-promo",
                              send_delay=0, argv=argv or [], start_jitter=0,
                              caps=None, account_status=lambda a: {x: True for x in a},
                              resolve_config=self.resolver(env))
        return rc, out.getvalue(), self.err.getvalue()

    def copy_of(self, **promos):
        """The fixture with some campaign documents replaced."""
        config = copy.deepcopy(self.membership)
        config["promos"].update(promos)
        return config
