"""Caption rotation and per-group tracking links in the shared promo engine
(workspace/lib/promo_broadcast.py).

The engine behaviour is unchanged; what the tests are handed is not. Each
campaign used to be a ``post_ad.py`` in this repository with its captions and
tracking in it and a generated ``targets.json`` beside it, so these tests
asserted on real copy and the real roster. Every campaign is one Firestore
document now, so the engine is driven from ``deploy/promo_fixture`` instead, and
the checks that were really about the operator's copy are gone with the copy --
noted where they were.

unittest-style (not pytest fixtures) so deploy/ci-check.sh, which runs each test
file with plain ``python3``, actually executes these.
"""

import io
import json
import os
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
for _path in (str(LIB_DIR), str(ROOT / "deploy")):
    if _path not in sys.path:
        sys.path.insert(0, _path)
import promo_broadcast as pb  # noqa: E402
import json_store  # noqa: E402
import gateway_campaigns  # noqa: E402
import promo_fixture  # noqa: E402

BOT_NUMBER = "+263785632943"
# The bot's second WhatsApp line (the "ads" account). A promo posted only to
# ads-account groups advertises this number via {bot_number}; replies still reach
# the bot (the same agent handles both lines), so its refs are recorded too.
ADS_NUMBER = "+263790453292"
# Both of the bot's own lines: a promo whose link resolves to either of these is a
# bot-line promo (copy uses {bot_number}, replies reach the ad-ref recorder), as
# opposed to one pinned to a third party's number.
BOT_LINES = frozenset({BOT_NUMBER, ADS_NUMBER})
# The line every bot-line promo advertises (membership.json
# defaults.contact_account). Pinned to the default line while the ads line is
# logged out of the gateway (since 2026-10-29): a wa.me link to an unlinked line
# reaches neither the public agent nor the ad-ref recorder.
CONTACT_ACCOUNT = "default"
CONTACT_NUMBER = BOT_NUMBER
# APPLIANCE REPAIRS PL's own WhatsApp number: the one promo whose link does not
# go to the bot, so its replies never reach the plugin's ad-ref recorder.
APPLIANCE_REPAIRS_NUMBER = "+263713911390"

# The reply recorder's prefix list, which is still code in this repository while
# the prefixes themselves are campaign data in Firestore.
PLUGIN_AD_REFS = ROOT / "workspace/plugins/public-service-gate/lib/ad-refs.js"


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
        # A campaign with fewer groups than variants can legitimately land them
        # all on one variant on a given day; the per-group daily rotation and
        # the no-consecutive-repeat guarantee are pinned separately. Spread is
        # only expected once there are at least as many groups as variants.
        for size in (3, 5, 14, 24, 40):
            jids = self.JIDS[:size]
            used = {pb.variant_index(j, 3, day=date(2026, 10, 9)) for j in jids}
            self.assertGreater(len(used), 1, "%d groups all got one variant" % size)

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
                state = Path(tmp, "state.json")
                rc = pb.broadcast(account_status=lambda accounts: {a: True for a in accounts}, text=self.VARIANTS, image=str(img),
                                  state_file=str(state), promo_name="alpha-promo",
                                  resolve_config=promo_fixture.resolver_for(
                                      self.JIDS[:6], captions=self.VARIANTS),
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
                rc = pb.broadcast(account_status=lambda accounts: {a: True for a in accounts}, text=self.VARIANTS, image=str(img),
                                  promo_name="alpha-promo",
                                  resolve_config=promo_fixture.resolver_for(
                                      self.JIDS[:12], captions=self.VARIANTS),
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
                with redirect_stdout(io.StringIO()):
                    rc = pb.broadcast(account_status=lambda accounts: {a: True for a in accounts}, text=self.VARIANTS, image=str(img),
                                      promo_name="alpha-promo",
                                      resolve_config=promo_fixture.resolver_for(
                                          self.JIDS[:3], captions=self.VARIANTS),
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
                text = "Fixed {not a placeholder} caption"
                pb.broadcast(account_status=lambda accounts: {a: True for a in accounts}, text=text, image=str(img),
                             promo_name="alpha-promo",
                             resolve_config=promo_fixture.resolver_for(
                                 ["1@g.us", "2@g.us"], captions=[text]),
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

    def test_a_suffix_identifies_one_group_at_the_scale_the_bot_runs_at(self):
        """One suffix must mean one group, whichever campaign printed it.

        This used to run over the committed roster -- every JID in every
        ``targets.json`` -- so it caught a real collision in the real group set.
        The roster is in Firestore now and no repository test can see it, so what
        is pinned here is that the scheme is adequate at the bot's scale: three
        characters of a 32-letter alphabet is 32768 suffixes, comfortable for
        around a hundred groups and NOT for thousands, where the birthday bound
        makes a collision likely. Growing far past this needs a longer code.
        """
        groups = 150                       # the bot is in ~91 today
        space = 32 ** pb.REF_CODE_LEN      # 32768 three-character suffixes
        # The birthday bound: fewer than one expected collision at this size.
        self.assertLess(groups ** 2 / (2 * space), 1.0)
        jids = ["1203630000%08d@g.us" % n for n in range(groups)]
        jids += ["26377%07d-15%08d@g.us" % (n, n) for n in range(groups)]
        for sample in (jids[:groups], jids[groups:]):
            suffixes = [pb.ref_code("X", jid)[2:] for jid in sample]
            self.assertEqual(len(set(suffixes)), len(suffixes), "suffix collision")
        # And the prefix never changes the suffix, so two campaigns in one group
        # print the same group identity.
        for jid in jids[:20]:
            self.assertEqual(pb.ref_code("BH", jid)[3:], pb.ref_code("GN", jid)[3:])

    def test_the_reply_recorder_reads_as_one_list_of_known_prefixes(self):
        """The recorder's half of the pair.

        Which prefixes exist is campaign data in Firestore, so comparing the two
        is a live check and belongs to the release gate
        (``gateway_campaigns.problems``, pinned in
        ``deploy/test_promo_send_spacing.py``). What is still checkable here is
        that the list the gate reads is the list the plugin matches on, and that
        both agree on the shape of a ref code.
        """
        src = PLUGIN_AD_REFS.read_text(encoding="utf-8")
        prefixes = gateway_campaigns.recorder_prefixes(PLUGIN_AD_REFS)
        self.assertTrue(prefixes)
        for prefix in prefixes:
            self.assertRegex(prefix, r"^[A-Z]{2,}$")
            self.assertRegex(pb.ref_code(prefix, "1@g.us"), r"^%s-[0-9A-HJKMNP-TV-Z]+$" % prefix)
        self.assertIn("[0-9A-HJKMNP-TV-Z]{%d}" % pb.REF_CODE_LEN, src)
        self.assertEqual(gateway_campaigns.AD_REFS, PLUGIN_AD_REFS)


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
                             state_file=str(Path(tmp, "s.json")), promo_name="alpha-promo",
                             resolve_config=promo_fixture.resolver_for(["1@g.us"]),
                             argv=[], tracking={"number": "+263785632943"})

    def test_bot_number_promo_requires_account_numbers(self):
        # A promo whose copy uses {bot_number} (or whose tracking pins no number)
        # must have an account_numbers map to resolve each group's advertised
        # number; a config with no line configured fails loudly rather than
        # posting an advert with a hole in it.
        saved = pb.send_ad
        pb.send_ad = lambda *a, **k: self.fail("must not send")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                img = Path(tmp, "a.png")
                img.write_bytes(b"img")
                resolve = promo_fixture.resolver_for(["1@g.us"], numbers={},
                                                     contact_account=None,
                                                     captions=["Call {bot_number}"])
                with self.assertRaises(ValueError):
                    pb.broadcast(account_status=lambda accounts: {a: True for a in accounts}, text="Call {bot_number}", image=str(img),
                                 state_file=str(Path(tmp, "s.json")), promo_name="alpha-promo",
                                 resolve_config=resolve,
                                 argv=[], start_jitter=0, caps=None)
        finally:
            pb.send_ad = saved


class AccountRoutingTests(unittest.TestCase):
    """Each WhatsApp line posts to its own groups and advertises its own number,
    so the promo group-load can be split across two accounts."""

    DEFAULT_NUM = BOT_NUMBER
    ADS_NUM = "+263790453292"
    # A bot promo pins no number: the link target comes from the posting account.
    TRACK = {"prefix": "GN", "label": "Genaie"}

    def _resolver(self, contact_account=None, captions=None, promo="alpha"):
        return promo_fixture.resolver_for(
            ["1@g.us", "2@g.us"], accounts={"2@g.us": "ads"},
            numbers={"default": self.DEFAULT_NUM, "ads": self.ADS_NUM},
            contact_account=contact_account, captions=captions, promo=promo)

    def _run(self, tmp, contact_account=None, **kwargs):
        sent = {}
        saved = pb.send_ad
        pb.send_ad = lambda target, text, image, account=None: (
            sent.__setitem__(target, (text, account)) or (True, '{"messageId":"m"}'))
        try:
            img = Path(tmp, "ad.png")
            img.write_bytes(b"img")
            kwargs.setdefault("promo_name", "alpha-promo")
            rc = pb.broadcast(account_status=lambda accounts: {a: True for a in accounts}, image=str(img),
                              state_file=str(Path(tmp, "s.json")),
                              resolve_config=self._resolver(
                                  contact_account, pb._variants(kwargs["text"]),
                                  promo=pb.promo_key(kwargs["promo_name"])),
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

    def test_a_campaign_with_no_groups_stays_a_quiet_no_op(self):
        # A campaign nobody has assigned a group to has nothing to send, and
        # must not be misreported as a configuration that failed to resolve.
        saved = pb.send_ad
        pb.send_ad = lambda *a, **k: self.fail("must not send")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                img = Path(tmp, "ad.png")
                img.write_bytes(b"img")
                with redirect_stdout(io.StringIO()) as buf:
                    rc = pb.broadcast(account_status=lambda accounts: {a: True for a in accounts}, text="Call {bot_number}", image=str(img),
                                      state_file=str(Path(tmp, "s.json")),
                                      resolve_config=promo_fixture.resolver_for([], promo="genaie"),
                                      tracking=self.TRACK, promo_name="genaie-promo",
                                      send_delay=0, argv=[], start_jitter=0, caps=None)
                self.assertEqual(rc, 0)
                self.assertIn("NO_REPLY", buf.getvalue())
        finally:
            pb.send_ad = saved


class EngineOutputTests(unittest.TestCase):
    """``--dry-run`` and ``--refs``, which an operator uses to check a campaign
    before letting it post.

    This class used to import the ten ``post_ad.py`` scripts and assert on their
    copy: how many variants each kept, that every price and phone number in the
    first variant reappeared in the rest, which number each advertised, and the
    whole dry-run output for the real roster. All of that was operator data in
    the repository and it is in Firestore now, so what is left here is the
    engine's own output, over a fixture. The copy itself is checked where it is
    now written -- the console's caption editor and
    ``operator_config.validate_captions`` -- which does NOT check that facts
    stay the same across variants; nothing does any more.
    """

    JIDS = ["12036300000000%04d@g.us" % n for n in range(1, 8)]
    TRACK = {"prefix": "GN", "label": "Genaie"}
    VARIANTS = ["First, call {bot_number}", "Second, call {bot_number}",
                "Third, call {bot_number}"]

    def run_engine(self, args):
        saved_date = pb.date
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp, "ad.png")
            image.write_bytes(b"img")
            state = Path(tmp, "state.json")
            buf = io.StringIO()
            pb.date = pin(date(2026, 10, 9))
            try:
                with redirect_stdout(buf):
                    rc = pb.broadcast(
                        text=self.VARIANTS, image=str(image), state_file=str(state),
                        promo_name="genaie-promo", tracking=self.TRACK,
                        resolve_config=promo_fixture.resolver_for(
                            self.JIDS, promo="genaie", captions=self.VARIANTS,
                            numbers={"default": CONTACT_NUMBER}),
                        account_status=lambda accounts: {a: True for a in accounts},
                        send_delay=0, start_jitter=0, cron_timeout=None, caps=None,
                        argv=args)
            finally:
                pb.date = saved_date
            # Neither flag may leave a receipt behind: a dry run that recorded a
            # send would silence the real one.
            self.assertFalse(state.exists(), "%r wrote state" % args)
        return rc, buf.getvalue()

    def test_dry_run_shows_variant_and_ref_per_group(self):
        saved = pb.send_ad
        pb.send_ad = lambda *a, **k: self.fail("--dry-run must not send")
        try:
            rc, out = self.run_engine(["--dry-run"])
        finally:
            pb.send_ad = saved
        self.assertEqual(rc, 0)
        count = len(self.VARIANTS)
        for jid in self.JIDS:
            line = next(l for l in out.splitlines() if jid in l)
            self.assertRegex(line, r"variant \d+/%d" % count)
            self.assertIn("ref %s" % pb.ref_code(self.TRACK["prefix"], jid), line)
            self.assertIn("contact %s" % CONTACT_NUMBER, line)
        self.assertIn("message (variant %d/%d)" % (count, count), out)
        self.assertIn("<ref_link>", out)

    def test_refs_flag_prints_mapping_without_sending(self):
        saved = pb.send_ad
        pb.send_ad = lambda *a, **k: self.fail("--refs must not send")
        try:
            rc, out = self.run_engine(["--refs"])
        finally:
            pb.send_ad = saved
        self.assertEqual(rc, 0)
        rows = [l.split("\t") for l in out.splitlines()[1:] if l.strip()]
        self.assertEqual({row[2] for row in rows}, set(self.JIDS))
        for ref, promo, jid, link in rows:
            self.assertEqual(promo, "genaie-promo")
            self.assertEqual(ref, pb.ref_code(self.TRACK["prefix"], jid))
            self.assertTrue(link.startswith(
                "https://wa.me/%s?text=" % pb.wa_digits(CONTACT_NUMBER)))

    def test_every_caption_variant_advertises_the_contact_line_and_no_other(self):
        for jid in self.JIDS:
            ref, link = pb.tracking_for(self.TRACK, jid, bot_number=CONTACT_NUMBER)
            for variant in self.VARIANTS:
                caption = pb.render_caption(variant, ref, link, pb.DEFAULT_LINK_LINE,
                                            bot_number=CONTACT_NUMBER)
                self.assertIn("wa.me/%s?" % pb.wa_digits(CONTACT_NUMBER), caption)
                for other in BOT_LINES - {CONTACT_NUMBER}:
                    self.assertNotIn(pb.wa_digits(other), caption)
                self.assertNotIn("{bot_number}", caption)


if __name__ == "__main__":
    unittest.main()
