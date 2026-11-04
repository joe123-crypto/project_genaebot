"""Caption rotation + per-group tracking links in the shared promo engine
(workspace/lib/promo_broadcast.py) and their wiring in the eight promo crons.

unittest-style (not pytest fixtures) so deploy/ci-check.sh, which runs each test
file with plain ``python3``, actually executes these.
"""

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = ROOT / "workspace/lib"
CRON = ROOT / "workspace/cron"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))
import promo_broadcast as pb  # noqa: E402
import json_store  # noqa: E402

BOT_NUMBER = "+263785632943"
# The bot's second WhatsApp line (the "ads" account). A promo posted only to
# ads-account groups advertises this number via {bot_number}; replies still reach
# the bot (the same agent handles both lines), so its refs are recorded too.
ADS_NUMBER = "+263790453292"
# Both of the bot's own lines: a promo whose link resolves to either of these is a
# bot-line promo (copy uses {bot_number}, replies reach the ad-ref recorder), as
# opposed to one pinned to a third party's number.
BOT_LINES = frozenset({BOT_NUMBER, ADS_NUMBER})
# APPLIANCE REPAIRS PL's own WhatsApp number: the one promo whose link does not
# go to the bot, so its replies never reach the plugin's ad-ref recorder.
APPLIANCE_REPAIRS_NUMBER = "+263713911390"

# promo dir -> (script, expected tracking prefix, expected link target)
PROMOS = {
    "appliance-repairs-promo": ("post_ad.py", "AR", APPLIANCE_REPAIRS_NUMBER),
    "borehole-drilling-promo": ("post_ad.py", "BH", ADS_NUMBER),
    "building-materials-promo": ("post_ad.py", "BM", ADS_NUMBER),
    "cv-promo": ("post_cv_ad.py", "CV", ADS_NUMBER),
    "fruit-trees-promo": ("post_ad.py", "FT", ADS_NUMBER),
    "genaie-advertisers-promo": ("post_ad.py", "GA", ADS_NUMBER),
    "genaie-leads-promo": ("post_ad.py", "GL", ADS_NUMBER),
    "genaie-promo": ("post_ad.py", "GN", ADS_NUMBER),
    "industrial-lighting-promo": ("post_ad.py", "IL", ADS_NUMBER),
    "skylight-solar-promo": ("post_ad.py", "SK", ADS_NUMBER),
}

PLUGIN_AD_REFS = ROOT / "workspace/plugins/public-service-gate/lib/ad-refs.js"


def load_promo(name):
    script, _, _ = PROMOS[name]
    spec = importlib.util.spec_from_file_location(
        "promo_" + name.replace("-", "_"), CRON / name / script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def all_target_jids(name):
    data = json.loads((CRON / name / "targets.json").read_text(encoding="utf-8"))
    jids = [g for g in data.get("groups", []) if g.endswith("@g.us")]
    return jids + [j for j in data.get("weekly_groups", {}) if j not in jids]


def facts(text):
    """Every number (prices, HP, sizes, quantities, phone digits, 24/7), dollar
    amount, %, link and the ``{bot_number}`` placeholder -- the facts that must
    not drift between caption variants. Spaces inside phone numbers
    ("+263 775 489 417") are collapsed first."""
    flat = re.sub(r"(?<=\d) (?=\d)", "", text)
    return set(re.findall(r"\$\d+|\+?\d+(?:\.\d+)?|%|genaie\.site|\{bot_number\}",
                          flat))


class _PinnedDate(date):
    _pinned = date(2026, 10, 5)  # a Monday

    @classmethod
    def today(cls):
        return cls._pinned


def pin(day):
    return type("Pinned", (_PinnedDate,), {"_pinned": day})


class VariantRotationTests(unittest.TestCase):
    VARIANTS = ["A", "B", "C"]
    JIDS = ["%d@g.us" % (120363000000000000 + i * 7919) for i in range(40)]

    def test_string_input_is_a_single_fixed_caption(self):
        self.assertEqual(pb._variants("hello"), ["hello"])
        idx, caption, ref = pb.caption_for("1@g.us", ["hello"], day=date(2026, 1, 1))
        self.assertEqual((idx, caption, ref), (0, "hello", None))

    def test_empty_variant_list_is_rejected(self):
        with self.assertRaises(ValueError):
            pb._variants(["", "  "])

    def test_rotation_is_deterministic(self):
        d = date(2026, 10, 9)
        first = [pb.variant_index(j, 3, day=d) for j in self.JIDS]
        second = [pb.variant_index(j, 3, day=d) for j in self.JIDS]
        self.assertEqual(first, second)

    def test_rotation_is_stable_across_processes(self):
        # Python's builtin hash() is salted per process; ours must not be.
        code = ("import sys; sys.path.insert(0, %r); import promo_broadcast as pb; "
                "from datetime import date; "
                "print(pb.variant_index('120363275485319265@g.us', 3, day=date(2026,10,9)), "
                "pb.ref_code('BH', '120363275485319265@g.us'))" % str(LIB_DIR))
        outs = set()
        for seed in ("1", "2", "random"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            outs.add(subprocess.run([sys.executable, "-c", code], env=env,
                                    capture_output=True, text=True, check=True).stdout)
        self.assertEqual(len(outs), 1, outs)
        self.assertIn(
            "%d %s" % (pb.variant_index("120363275485319265@g.us", 3, day=date(2026, 10, 9)),
                       pb.ref_code("BH", "120363275485319265@g.us")),
            outs.pop())

    def test_daily_group_never_repeats_caption_on_consecutive_days(self):
        start = date(2026, 1, 1)
        for jid in self.JIDS:
            prev = None
            for k in range(60):
                idx = pb.variant_index(jid, 3, day=start + timedelta(days=k))
                self.assertNotEqual(idx, prev, jid)
                prev = idx

    def test_weekly_group_never_repeats_caption_on_consecutive_posts(self):
        allowed = frozenset({0, 3})  # Mon, Thu (Vacancy Lamp)
        for n in (2, 3):
            for jid in self.JIDS[:10]:
                prev = None
                day = date(2026, 1, 1)
                for _ in range(120):
                    if day.weekday() in allowed:
                        idx = pb.variant_index(jid, n, day=day, allowed=allowed)
                        self.assertNotEqual(idx, prev, (jid, n, day))
                        prev = idx
                    day += timedelta(days=1)

    def test_groups_are_spread_across_variants_on_a_day(self):
        for name in PROMOS:
            jids = all_target_jids(name)
            # A promo with fewer groups than variants can legitimately land them
            # all on one variant on a given day (big-player posts to only two
            # ads-line groups); the per-group daily rotation and no-consecutive-
            # repeat guarantees are pinned separately. Spread across variants is
            # only expected once a promo has at least as many groups as variants.
            if len(jids) < 3:
                continue
            used = {pb.variant_index(j, 3, day=date(2026, 10, 9)) for j in jids}
            self.assertGreater(len(used), 1, "%s: every group got the same variant" % name)

    def test_send_loop_sends_per_group_variant(self):
        sent = {}
        saved_send, saved_date = pb.send_ad, pb.date
        pb.send_ad = lambda target, text, image, account=None: (
            sent.__setitem__(target, text) or (True, '{"messageId":"m"}'))
        pb.date = pin(date(2026, 10, 9))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                img = Path(tmp, "ad.png")
                img.write_bytes(b"img")
                targets = Path(tmp, "targets.json")
                targets.write_text(json.dumps({"groups": self.JIDS[:6]}))
                state = Path(tmp, "state.json")
                rc = pb.broadcast(account_status=lambda accounts: {a: True for a in accounts}, text=self.VARIANTS, image=str(img),
                                  targets_file=str(targets), state_file=str(state),
                                  send_delay=0, argv=[], start_jitter=0, caps=None)
                self.assertEqual(rc, 0)
                for jid in self.JIDS[:6]:
                    expected = self.VARIANTS[pb.variant_index(jid, 3, day=date(2026, 10, 9))]
                    self.assertEqual(sent[jid], expected)
                self.assertEqual(set(json_store.load_json(str(state), {})["sent"]),
                                 set(self.JIDS[:6]))
        finally:
            pb.send_ad, pb.date = saved_send, saved_date

    def test_variant_images_pin_an_image_to_a_variant(self):
        sent = {}
        saved_send, saved_date = pb.send_ad, pb.date
        pb.send_ad = lambda target, text, image, account=None: (
            sent.__setitem__(target, image) or (True, '{"messageId":"m"}'))
        pb.date = pin(date(2026, 10, 9))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                img, pinned = Path(tmp, "ad.png"), Path(tmp, "pinned.png")
                img.write_bytes(b"img")
                pinned.write_bytes(b"img")
                targets = Path(tmp, "targets.json")
                targets.write_text(json.dumps({"groups": self.JIDS[:12]}))
                rc = pb.broadcast(account_status=lambda accounts: {a: True for a in accounts}, text=self.VARIANTS, image=str(img),
                                  targets_file=str(targets),
                                  state_file=str(Path(tmp, "state.json")),
                                  send_delay=0, argv=[], start_jitter=0, caps=None,
                                  variant_images={1: str(pinned)})
                self.assertEqual(rc, 0)
                idxs = {j: pb.variant_index(j, 3, day=date(2026, 10, 9))
                        for j in self.JIDS[:12]}
                self.assertIn(1, idxs.values())
                self.assertNotEqual(set(idxs.values()), {1})
                for jid, idx in idxs.items():
                    self.assertEqual(sent[jid], str(pinned if idx == 1 else img), jid)
        finally:
            pb.send_ad, pb.date = saved_send, saved_date

    def test_missing_variant_image_fails_before_sending(self):
        saved = pb.send_ad
        pb.send_ad = lambda *a: self.fail("must not send")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                img = Path(tmp, "ad.png")
                img.write_bytes(b"img")
                Path(tmp, "t.json").write_text(json.dumps({"groups": self.JIDS[:3]}))
                with redirect_stdout(io.StringIO()):
                    rc = pb.broadcast(account_status=lambda accounts: {a: True for a in accounts}, text=self.VARIANTS, image=str(img),
                                      targets_file=str(Path(tmp, "t.json")),
                                      state_file=str(Path(tmp, "s.json")),
                                      send_delay=0, argv=[], start_jitter=0, caps=None,
                                      variant_images={2: str(Path(tmp, "nope.png"))})
                self.assertEqual(rc, 1)
        finally:
            pb.send_ad = saved

    def test_string_caption_is_sent_verbatim(self):
        sent = []
        saved = pb.send_ad
        pb.send_ad = lambda target, text, image, account=None: (sent.append(text) or (True, '{"messageId":"m"}'))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                img = Path(tmp, "ad.png")
                img.write_bytes(b"img")
                targets = Path(tmp, "targets.json")
                targets.write_text(json.dumps({"groups": ["1@g.us", "2@g.us"]}))
                text = "Fixed {not a placeholder} caption"
                pb.broadcast(account_status=lambda accounts: {a: True for a in accounts}, text=text, image=str(img), targets_file=str(targets),
                             state_file=str(Path(tmp, "s.json")), send_delay=0, argv=[], start_jitter=0, caps=None)
                self.assertEqual(sent, [text, text])
        finally:
            pb.send_ad = saved


class RefCodeTests(unittest.TestCase):
    def test_format_and_alphabet(self):
        ref = pb.ref_code("bh", "120363275485319265@g.us")
        self.assertRegex(ref, r"^BH-[0-9A-HJKMNP-TV-Z]{3}$")

    def test_stable_value(self):
        # Pin one value so an accidental algorithm change (which would orphan
        # every printed ref) is caught.
        self.assertEqual(pb.ref_code("BH", "120363275485319265@g.us"), "BH-VD4")
        self.assertEqual(pb.ref_code("BH", "120363275485319265@g.us")[3:],
                         pb.ref_code("GN", "120363275485319265@g.us")[3:])

    def test_unique_across_current_targets(self):
        for name, (_, prefix, _) in PROMOS.items():
            jids = all_target_jids(name)
            refs = [pb.ref_code(prefix, j) for j in jids]
            self.assertEqual(len(set(refs)), len(refs), "%s: ref collision" % name)
        # Also unique across the union of every group the bot posts to, so one
        # suffix always identifies one group whichever promo it came from.
        union = sorted({j for name in PROMOS for j in all_target_jids(name)})
        suffixes = [pb.ref_code("X", j)[2:] for j in union]
        self.assertEqual(len(set(suffixes)), len(suffixes), "suffix collision across groups")

    def test_plugin_prefix_list_matches_promos(self):
        src = PLUGIN_AD_REFS.read_text(encoding="utf-8")
        m = re.search(r"AD_REF_PREFIXES = \[([^\]]*)\]", src)
        self.assertIsNotNone(m)
        plugin_prefixes = set(re.findall(r'"([A-Z]+)"', m.group(1)))
        # Only replies sent to the bot can be counted by the plugin, so a promo
        # that links to a third party's number stays out of its prefix list.
        self.assertEqual(plugin_prefixes,
                         {p for _, p, n in PROMOS.values() if n in BOT_LINES} | {"BP", "PB"})
        self.assertIn("[0-9A-HJKMNP-TV-Z]{%d}" % pb.REF_CODE_LEN, src)


class LinkAndPlaceholderTests(unittest.TestCase):
    TRACK = {"prefix": "BH", "number": "+263 78 563 2943", "label": "borehole"}

    def test_link_is_wa_me_with_encoded_prefill(self):
        link = pb.ref_link("+263 78-563 2943", "Hi, I saw your borehole ad (ref BH-7K2) & more?")
        self.assertTrue(link.startswith("https://wa.me/263785632943?text="))
        query = link.split("?text=", 1)[1]
        self.assertNotIn(" ", query)
        self.assertNotIn("&", query)
        self.assertNotIn("?", query)
        self.assertEqual(urllib.parse.unquote(query),
                         "Hi, I saw your borehole ad (ref BH-7K2) & more?")

    def test_tracking_for_uses_default_prefill(self):
        ref, link = pb.tracking_for(self.TRACK, "1@g.us")
        self.assertEqual(ref, pb.ref_code("BH", "1@g.us"))
        text = urllib.parse.unquote(link.split("?text=", 1)[1])
        self.assertEqual(text, "Hi, I saw your borehole ad (ref %s)" % ref)

    def test_placeholders_substituted(self):
        out = pb.render_caption("Call us {ref_link}\nCode: {ref}", "BH-7K2", "https://x")
        self.assertEqual(out, "Call us https://x\nCode: BH-7K2")

    def test_link_line_appended_when_no_placeholder(self):
        out = pb.render_caption("Body\n", "BH-7K2", "https://x")
        self.assertEqual(out, "Body\n\n" + pb.DEFAULT_LINK_LINE.replace("{ref_link}", "https://x"))

    def test_untracked_caption_unchanged_even_with_braces(self):
        self.assertEqual(pb.render_caption("a {ref} {x}"), "a {ref} {x}")

    def test_tracking_requires_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                pb.broadcast(account_status=lambda accounts: {a: True for a in accounts}, text="x", image=str(Path(tmp, "a.png")),
                             targets_file=str(Path(tmp, "t.json")),
                             state_file=str(Path(tmp, "s.json")),
                             argv=[], tracking={"number": "+263785632943"})

    def test_bot_number_promo_requires_account_numbers(self):
        # A promo whose copy uses {bot_number} (or whose tracking pins no number)
        # must have an account_numbers map to resolve each group's advertised
        # number; an un-regenerated targets.json that lacks it fails loudly.
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp, "a.png")
            img.write_bytes(b"img")
            targets = Path(tmp, "t.json")
            targets.write_text(json.dumps({"groups": ["1@g.us"]}))
            with self.assertRaises(ValueError):
                pb.broadcast(account_status=lambda accounts: {a: True for a in accounts}, text="Call {bot_number}", image=str(img),
                             targets_file=str(targets),
                             state_file=str(Path(tmp, "s.json")),
                             argv=[], start_jitter=0, caps=None)


class AccountRoutingTests(unittest.TestCase):
    """Each WhatsApp line posts to its own groups and advertises its own number,
    so the promo group-load can be split across two accounts."""

    DEFAULT_NUM = BOT_NUMBER
    ADS_NUM = "+263790453292"
    # A bot promo pins no number: the link target comes from the posting account.
    TRACK = {"prefix": "GN", "label": "Genaie"}

    def _targets(self, tmp, contact_account=None):
        targets = Path(tmp, "targets.json")
        payload = {
            "groups": ["1@g.us", "2@g.us"],
            "accounts": {"2@g.us": "ads"},
            "account_numbers": {"default": self.DEFAULT_NUM, "ads": self.ADS_NUM},
        }
        if contact_account is not None:
            payload["contact_account"] = contact_account
        targets.write_text(json.dumps(payload))
        return targets

    def _run(self, tmp, contact_account=None, **kwargs):
        sent = {}
        saved = pb.send_ad
        pb.send_ad = lambda target, text, image, account=None: (
            sent.__setitem__(target, (text, account)) or (True, '{"messageId":"m"}'))
        try:
            img = Path(tmp, "ad.png")
            img.write_bytes(b"img")
            rc = pb.broadcast(account_status=lambda accounts: {a: True for a in accounts}, image=str(img), targets_file=str(self._targets(tmp, contact_account)),
                              state_file=str(Path(tmp, "s.json")),
                              send_delay=0, argv=[], start_jitter=0, caps=None,
                              **kwargs)
        finally:
            pb.send_ad = saved
        return rc, sent

    def test_only_a_non_default_account_is_passed_to_the_gateway(self):
        # The gateway rejects an account id it has no registry entry for
        # ("Unknown account ..."), and channels.whatsapp.accounts is legitimately
        # absent on a single-account gateway, so the default must stay implicit or
        # every send breaks there. Correct routing for the default line comes from
        # defaultAccount in config instead (asserted below).
        seen = {}

        class _Done:
            returncode = 0
            stdout = '{"messageId":"m"}'
            stderr = ""

        saved = pb.subprocess.run
        pb.subprocess.run = lambda cmd, **kw: (seen.__setitem__("cmd", cmd) or _Done())
        try:
            ok, _ = pb.send_ad("1@g.us", "hi", str(Path(os.devnull)), account="ads")
            self.assertTrue(ok)
            cmd = seen["cmd"]
            self.assertEqual(cmd[cmd.index("--account") + 1], "ads")

            for implicit in (pb.DEFAULT_ACCOUNT, None):
                seen.clear()
                ok, _ = pb.send_ad("1@g.us", "hi", str(Path(os.devnull)),
                                   account=implicit)
                self.assertTrue(ok, implicit)
                self.assertNotIn("--account", seen["cmd"], implicit)
        finally:
            pb.subprocess.run = saved

    def test_config_pins_the_default_account(self):
        # send_ad leaves the default account implicit, so the config must say
        # which account that is: without defaultAccount the gateway picks
        # "default" if present else the first id sorted, and "ads" sorts first --
        # which would post every untagged group from the second number.
        wa = json.loads((ROOT / "config/openclaw.template.json").read_text(
            encoding="utf-8"))["channels"]["whatsapp"]
        self.assertEqual(wa.get("defaultAccount"), pb.DEFAULT_ACCOUNT)
        self.assertIn(pb.DEFAULT_ACCOUNT, wa.get("accounts") or {})

    def test_each_group_posts_from_and_advertises_its_own_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, sent = self._run(tmp, text="Call {bot_number}", tracking=self.TRACK,
                                 promo_name="genaie-promo")
        self.assertEqual(rc, 0)
        default_text, default_account = sent["1@g.us"]
        ads_text, ads_account = sent["2@g.us"]
        self.assertEqual(default_account, "default")
        self.assertEqual(ads_account, "ads")
        for text, mine, theirs in ((default_text, self.DEFAULT_NUM, self.ADS_NUM),
                                   (ads_text, self.ADS_NUM, self.DEFAULT_NUM)):
            self.assertIn(mine, text)                       # copy advertises itself
            self.assertIn("wa.me/%s?" % pb.wa_digits(mine), text)   # so does the link
            self.assertNotIn(theirs, text)                  # and never the other line
            self.assertNotIn("wa.me/%s?" % pb.wa_digits(theirs), text)

    def test_shared_contact_keeps_each_sending_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, sent = self._run(tmp, contact_account="ads", text="Call {bot_number}",
                                 tracking=self.TRACK, promo_name="genaie-promo")
        self.assertEqual(rc, 0)
        for jid, account in (("1@g.us", "default"), ("2@g.us", "ads")):
            text, sender = sent[jid]
            self.assertEqual(sender, account)
            self.assertIn(self.ADS_NUM, text)
            self.assertIn("wa.me/%s?" % pb.wa_digits(self.ADS_NUM), text)
            self.assertNotIn(pb.wa_digits(self.DEFAULT_NUM), text)
            self.assertIn(pb.ref_code("GN", jid), text)

    def test_invalid_shared_contact_fails_before_send(self):
        for invalid in ("missing", "", [], 42):
            with self.subTest(contact=invalid), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(ValueError, "contact_account"):
                    self._run(tmp, contact_account=invalid, text="Call {bot_number}",
                              tracking=self.TRACK, promo_name="genaie-promo")
                self.assertFalse(Path(tmp, "s.json").exists())

    def test_pinned_tracking_number_wins_over_the_posting_account(self):
        # appliance-repairs routes replies to a fixed shop line whichever account
        # posts the ad, so a per-account number must not override it.
        shop = APPLIANCE_REPAIRS_NUMBER
        with tempfile.TemporaryDirectory() as tmp:
            rc, sent = self._run(tmp, contact_account="ads", text="Repairs",
                                 tracking={"prefix": "AR", "number": shop,
                                           "label": "repairs"},
                                 promo_name="appliance-repairs-promo")
        self.assertEqual(rc, 0)
        ads_text, ads_account = sent["2@g.us"]
        self.assertEqual(ads_account, "ads")  # posted from the second line...
        # ...but the link still goes to the shop, not to that line's number.
        self.assertIn("wa.me/%s?" % pb.wa_digits(shop), ads_text)
        self.assertNotIn("wa.me/%s?" % pb.wa_digits(self.ADS_NUM), ads_text)

    def test_missing_targets_file_stays_a_quiet_no_op(self):
        # A missing/unreadable targets file has nothing to send and must not be
        # misreported as a stale, un-regenerated one.
        saved = pb.send_ad
        pb.send_ad = lambda *a, **k: self.fail("must not send")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                img = Path(tmp, "ad.png")
                img.write_bytes(b"img")
                with redirect_stdout(io.StringIO()) as buf:
                    rc = pb.broadcast(account_status=lambda accounts: {a: True for a in accounts}, text="Call {bot_number}", image=str(img),
                                      targets_file=str(Path(tmp, "missing.json")),
                                      state_file=str(Path(tmp, "s.json")),
                                      tracking=self.TRACK, promo_name="genaie-promo",
                                      send_delay=0, argv=[], start_jitter=0, caps=None)
                self.assertEqual(rc, 0)
                self.assertIn("NO_REPLY", buf.getvalue())
        finally:
            pb.send_ad = saved


class PromoWiringTests(unittest.TestCase):
    def test_each_promo_keeps_original_first_and_expected_variant_count(self):
        for name in PROMOS:
            m = load_promo(name)
            if name == "genaie-advertisers-promo":
                self.assertEqual(len(m.POST_TEXTS), 1, name)
            else:
                self.assertGreaterEqual(len(m.POST_TEXTS), 3, name)
            self.assertIs(m.POST_TEXTS[0], m.POST_TEXT, name)
            self.assertEqual(len(set(m.POST_TEXTS)), len(m.POST_TEXTS),
                             "%s: duplicate variants" % name)

    def test_variants_keep_every_fact(self):
        for name in PROMOS:
            m = load_promo(name)
            base = facts(m.POST_TEXT)
            if name != "genaie-advertisers-promo":
                self.assertTrue(base, name)
            for k, v in enumerate(m.POST_TEXTS[1:], start=2):
                # Cited source links (e.g. the borehole El Nino variant's forecast
                # URL) are the only extra content a variant may carry.
                for url in getattr(m, "SOURCE_URLS", ()):
                    v = v.replace(url, "")
                self.assertEqual(facts(v), base, "%s variant %d facts drifted" % (name, k))

    def test_advertiser_campaign_sends_selected_groups_once_daily_on_their_accounts(self):
        m = load_promo("genaie-advertisers-promo")
        expected = {
            "263782033305-1606361015@g.us": "ads",
            "120363412236304347@g.us": "ads",
            "263713611554-1588562864@g.us": "ads",
            "120363415810487005@g.us": "ads",
            "263779541998-1547060119@g.us": "ads",
            "120363412512900877@g.us": "default",
            "263773491386-1531974442@g.us": "ads",
            "263773491386-1578547813@g.us": "ads",
            "263773491386-1578547921@g.us": "ads",
            "263776554037-1383758077@g.us": "ads",
        }
        sent = []
        saved = pb.send_ad
        pb.send_ad = lambda target, text, image, account=None: (
            sent.append((target, text, image, account)) or (True, '{"messageId":"m"}'))
        try:
            with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
                kwargs = dict(text=m.POST_TEXTS, image=m.IMAGE,
                              targets_file=m.TARGETS_FILE,
                              state_file=str(Path(tmp, "state.json")),
                              tracking=m.TRACKING, promo_name="genaie-advertisers-promo",
                              send_delay=0, start_jitter=0, cron_timeout=None,
                              caps=None, argv=[])
                self.assertEqual(pb.broadcast(account_status=lambda accounts: {a: True for a in accounts}, **kwargs), 0)
                self.assertEqual(pb.broadcast(account_status=lambda accounts: {a: True for a in accounts}, **kwargs), 0)
            self.assertEqual(len(sent), len(expected))
            self.assertEqual({target for target, *_ in sent}, set(expected))
            for target, text, image, account in sent:
                self.assertEqual(account, expected[target])
                self.assertEqual(image, m.IMAGE)
                self.assertTrue(text.startswith(m.POST_TEXT + "\n\n"))
                self.assertIn("wa.me/263790453292?text=", text)
                self.assertIn(pb.ref_code("GA", target), text)
        finally:
            pb.send_ad = saved

    def test_borehole_general_caption_and_four_photos(self):
        m = load_promo("borehole-drilling-promo")
        self.assertEqual(len(m.POST_TEXTS), 3)
        self.assertEqual(len(m._IMAGE_ASSETS), 4)
        for caption in m.POST_TEXTS:
            self.assertNotIn("$", caption)
            self.assertIn("Zimbabwe", caption)
            self.assertIn("{bot_number}", caption)
            self.assertNotIn("El Nino", caption)
        self.assertFalse(hasattr(m, "VARIANT_IMAGES"))

    def test_tracking_targets(self):
        for name, (_, prefix, number) in PROMOS.items():
            m = load_promo(name)
            self.assertEqual(m.TRACKING["prefix"], prefix, name)
            if number in BOT_LINES:
                # Bot promos use the configured contact account, substituted
                # into the {bot_number} placeholder in the copy.
                self.assertIsNone(m.TRACKING.get("number"), name)
                if name != "genaie-advertisers-promo":
                    self.assertIn("{bot_number}", m.POST_TEXT, name)
            else:
                # A promo that pins its own contact (appliance-repairs -> shop)
                # keeps that number, and it must be printed in the ad copy.
                self.assertEqual(m.TRACKING["number"], number, name)
                digits = pb.wa_digits(number)
                printed = pb.wa_digits(m.POST_TEXT)
                self.assertTrue(
                    digits in printed
                    or (digits.startswith("263") and "0" + digits[3:] in printed),
                    "%s: link number is not in the ad" % name)

    def _run(self, name, args, day=date(2026, 10, 9)):
        m = load_promo(name)
        saved_argv, saved_date = sys.argv, pb.date
        with tempfile.TemporaryDirectory() as tmp:
            state_env = {
                "appliance-repairs-promo": "APPLIANCE_REPAIRS_STATE",
                "borehole-drilling-promo": "BOREHOLE_STATE",
                "building-materials-promo": "BUILDING_MATERIALS_STATE",
                "cv-promo": "CV_PROMO_STATE",
                "fruit-trees-promo": "FRUIT_TREES_STATE",
                "genaie-leads-promo": "GENAIE_LEADS_PROMO_STATE",
                "genaie-advertisers-promo": "GENAIE_ADVERTISERS_PROMO_STATE",
                "genaie-promo": "GENAIE_PROMO_STATE",
                "industrial-lighting-promo": "INDUSTRIAL_LIGHTING_PROMO_STATE",
                "skylight-solar-promo": "SKYLIGHT_STATE",
            }[name]
            m.STATE_FILE = str(Path(tmp, "state.json"))
            sys.argv = [name] + args
            pb.date = pin(day)
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    rc = m.main()
            finally:
                sys.argv, pb.date = saved_argv, saved_date
            self.assertFalse(Path(m.STATE_FILE).exists(), "%s wrote state" % state_env)
        return rc, buf.getvalue()

    def test_dry_run_shows_variant_and_ref_per_group(self):
        for name, (_, prefix, number) in PROMOS.items():
            rc, out = self._run(name, ["--dry-run"])
            self.assertEqual(rc, 0, name)
            n = len(load_promo(name).POST_TEXTS)
            for jid in all_target_jids(name):
                line = next(l for l in out.splitlines() if jid in l)
                if n > 1:
                    self.assertRegex(line, r"variant \d+/%d" % n, name)
                self.assertIn("ref %s" % pb.ref_code(prefix, jid), line)
                self.assertIn("contact %s" % number, line)
            self.assertIn("message (variant %d/%d)" % (n, n) if n > 1 else "----- message -----", out)
            self.assertIn("<ref_link>", out)

    def test_all_production_caption_variants_use_the_expected_contact(self):
        for name, (_, prefix, number) in PROMOS.items():
            m = load_promo(name)
            targets = json.loads(Path(m.TARGETS_FILE).read_text())
            self.assertEqual(targets["contact_account"], "ads", name)
            selected = m.TRACKING.get("number") or targets["account_numbers"][targets["contact_account"]]
            self.assertEqual(selected, number, name)
            for jid in all_target_jids(name):
                ref, link = pb.tracking_for(m.TRACKING, jid, bot_number=selected)
                for variant in m.POST_TEXTS:
                    caption = pb.render_caption(variant, ref, link, pb.DEFAULT_LINK_LINE,
                                                bot_number=selected)
                    self.assertIn("wa.me/%s?" % pb.wa_digits(number), caption, name)
                    self.assertNotIn(pb.wa_digits(BOT_NUMBER), caption, name)
                    self.assertNotIn("{bot_number}", caption, name)

    def test_refs_flag_prints_mapping_without_sending(self):
        saved = pb.send_ad
        pb.send_ad = lambda *a: self.fail("--refs must not send")
        try:
            for name, (_, prefix, number) in PROMOS.items():
                rc, out = self._run(name, ["--refs"])
                self.assertEqual(rc, 0)
                rows = [l.split("\t") for l in out.splitlines()[1:] if l.strip()]
                self.assertEqual({r[2] for r in rows}, set(all_target_jids(name)))
                for ref, promo, jid, link in rows:
                    self.assertEqual(promo, name)
                    self.assertEqual(ref, pb.ref_code(prefix, jid))
                    self.assertTrue(link.startswith("https://wa.me/%s?text=" % pb.wa_digits(number)))
        finally:
            pb.send_ad = saved


if __name__ == "__main__":
    unittest.main()
