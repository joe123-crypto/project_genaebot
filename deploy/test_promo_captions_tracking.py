"""Caption rotation + per-group tracking links in the shared promo engine
(workspace/lib/promo_broadcast.py) and their wiring in the six promo crons.

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

# promo dir -> (script, expected tracking prefix, expected link target)
PROMOS = {
    "borehole-drilling-promo": ("post_ad.py", "BH", BOT_NUMBER),
    "building-materials-promo": ("post_ad.py", "BM", BOT_NUMBER),
    "cv-promo": ("post_cv_ad.py", "CV", BOT_NUMBER),
    "fruit-trees-promo": ("post_ad.py", "FT", BOT_NUMBER),
    "genaie-leads-promo": ("post_ad.py", "GL", BOT_NUMBER),
    "genaie-promo": ("post_ad.py", "GN", BOT_NUMBER),
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
    amount, % and link -- the facts that must not drift between caption variants.
    Spaces inside phone numbers ("+263 775 489 417") are collapsed first."""
    flat = re.sub(r"(?<=\d) (?=\d)", "", text)
    return set(re.findall(r"\$\d+|\+?\d+(?:\.\d+)?|%|genaie\.site", flat))


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
            used = {pb.variant_index(j, 3, day=date(2026, 10, 9)) for j in jids}
            self.assertGreater(len(used), 1, "%s: every group got the same variant" % name)

    def test_send_loop_sends_per_group_variant(self):
        sent = {}
        saved_send, saved_date = pb.send_ad, pb.date
        pb.send_ad = lambda target, text, image: (sent.__setitem__(target, text)
                                                  or (True, '{"messageId":"m"}'))
        pb.date = pin(date(2026, 10, 9))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                img = Path(tmp, "ad.png")
                img.write_bytes(b"img")
                targets = Path(tmp, "targets.json")
                targets.write_text(json.dumps({"groups": self.JIDS[:6]}))
                state = Path(tmp, "state.json")
                rc = pb.broadcast(text=self.VARIANTS, image=str(img),
                                  targets_file=str(targets), state_file=str(state),
                                  send_delay=0, argv=[])
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
        pb.send_ad = lambda target, text, image: (sent.__setitem__(target, image)
                                                  or (True, '{"messageId":"m"}'))
        pb.date = pin(date(2026, 10, 9))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                img, pinned = Path(tmp, "ad.png"), Path(tmp, "pinned.png")
                img.write_bytes(b"img")
                pinned.write_bytes(b"img")
                targets = Path(tmp, "targets.json")
                targets.write_text(json.dumps({"groups": self.JIDS[:12]}))
                rc = pb.broadcast(text=self.VARIANTS, image=str(img),
                                  targets_file=str(targets),
                                  state_file=str(Path(tmp, "state.json")),
                                  send_delay=0, argv=[],
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
                    rc = pb.broadcast(text=self.VARIANTS, image=str(img),
                                      targets_file=str(Path(tmp, "t.json")),
                                      state_file=str(Path(tmp, "s.json")),
                                      send_delay=0, argv=[],
                                      variant_images={2: str(Path(tmp, "nope.png"))})
                self.assertEqual(rc, 1)
        finally:
            pb.send_ad = saved

    def test_string_caption_is_sent_verbatim(self):
        sent = []
        saved = pb.send_ad
        pb.send_ad = lambda target, text, image: (sent.append(text) or (True, '{"messageId":"m"}'))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                img = Path(tmp, "ad.png")
                img.write_bytes(b"img")
                targets = Path(tmp, "targets.json")
                targets.write_text(json.dumps({"groups": ["1@g.us", "2@g.us"]}))
                text = "Fixed {not a placeholder} caption"
                pb.broadcast(text=text, image=str(img), targets_file=str(targets),
                             state_file=str(Path(tmp, "s.json")), send_delay=0, argv=[])
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
        self.assertEqual(plugin_prefixes, {p for _, p, _ in PROMOS.values()})
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

    def test_tracking_requires_prefix_and_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                pb.broadcast(text="x", image=str(Path(tmp, "a.png")),
                             targets_file=str(Path(tmp, "t.json")),
                             state_file=str(Path(tmp, "s.json")),
                             argv=[], tracking={"prefix": "BH"})


class PromoWiringTests(unittest.TestCase):
    def test_each_promo_has_three_variants_with_original_first(self):
        for name in PROMOS:
            m = load_promo(name)
            self.assertGreaterEqual(len(m.POST_TEXTS), 3, name)
            self.assertIs(m.POST_TEXTS[0], m.POST_TEXT, name)
            self.assertEqual(len(set(m.POST_TEXTS)), len(m.POST_TEXTS),
                             "%s: duplicate variants" % name)

    def test_variants_keep_every_fact(self):
        for name in PROMOS:
            m = load_promo(name)
            base = facts(m.POST_TEXT)
            self.assertTrue(base, name)
            for k, v in enumerate(m.POST_TEXTS[1:], start=2):
                # Cited source links (e.g. the borehole El Nino variant's forecast
                # URL) are the only extra content a variant may carry.
                for url in getattr(m, "SOURCE_URLS", ()):
                    v = v.replace(url, "")
                self.assertEqual(facts(v), base, "%s variant %d facts drifted" % (name, k))

    def test_borehole_elnino_variant(self):
        m = load_promo("borehole-drilling-promo")
        v4 = m.POST_TEXT_V4
        self.assertIn(m.POST_TEXT_V4, m.POST_TEXTS)
        self.assertIn("*El Nino is coming. Are you prepared?*", v4.splitlines()[0])
        self.assertIn(m.FORECAST_URL, v4)
        self.assertTrue(m.FORECAST_URL.startswith("https://www.weatherzw.org.zw/"))
        self.assertTrue(os.path.isfile(m.ELNINO_IMAGE), m.ELNINO_IMAGE)
        if not os.environ.get("BOREHOLE_IMAGE"):
            self.assertEqual(m.VARIANT_IMAGES, {m.POST_TEXTS.index(v4): m.ELNINO_IMAGE})

    def test_tracking_targets(self):
        for name, (_, prefix, number) in PROMOS.items():
            m = load_promo(name)
            self.assertEqual(m.TRACKING["prefix"], prefix, name)
            self.assertEqual(m.TRACKING["number"], number, name)
            # The link must point at a number the ad itself already advertises.
            self.assertIn(pb.wa_digits(number), pb.wa_digits(m.POST_TEXT), name)

    def _run(self, name, args, day=date(2026, 10, 9)):
        m = load_promo(name)
        saved_argv, saved_date = sys.argv, pb.date
        with tempfile.TemporaryDirectory() as tmp:
            state_env = {
                "borehole-drilling-promo": "BOREHOLE_STATE",
                "building-materials-promo": "BUILDING_MATERIALS_STATE",
                "cv-promo": "CV_PROMO_STATE",
                "fruit-trees-promo": "FRUIT_TREES_STATE",
                "genaie-leads-promo": "GENAIE_LEADS_PROMO_STATE",
                "genaie-promo": "GENAIE_PROMO_STATE",
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
        for name, (_, prefix, _) in PROMOS.items():
            rc, out = self._run(name, ["--dry-run"])
            self.assertEqual(rc, 0, name)
            n = len(load_promo(name).POST_TEXTS)
            for jid in all_target_jids(name):
                line = next(l for l in out.splitlines() if jid in l)
                self.assertRegex(line, r"variant \d+/%d" % n, name)
                self.assertIn("ref %s" % pb.ref_code(prefix, jid), line)
            self.assertIn("message (variant %d/%d)" % (n, n), out)
            self.assertIn("<ref_link>", out)

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
