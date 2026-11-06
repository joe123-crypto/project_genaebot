"""Regression coverage for PR #169's config resolution and cap predictions."""
import copy
import importlib.machinery
import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'workspace/lib'))
from test_promo_cutover import Harness, DAY
import promo_caps
import promo_config
import promo_membership


def command(name):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / 'workspace/bin' / name))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class CapPredictions(unittest.TestCase):
    def test_paused_campaign_slot_is_reallocated_everywhere(self):
        console = command('console-read')
        listing = command('list-groups')
        declared = copy.deepcopy(console.manifest())
        for campaign in declared['promos']:
            campaign['enabled'] = True
        console.manifest = lambda: declared
        m = copy.deepcopy(promo_membership.load(console.MEMBERSHIP))
        jid = next(j for j, e in m['groups'].items()
                   if promo_membership.status_of(e) == 'active')
        entry = m['groups'][jid]
        m['groups'] = {jid: entry}
        entry.update(promos=['cv', 'genaie'], daily_cap=1, weekly={})
        with patch.object(promo_caps, 'load_ad_refs', return_value={}), \
                patch.object(promo_caps, 'previous_scores', return_value=None):
            initial = promo_caps.plan(m, [jid], day=DAY, disabled=set())[jid]['allowed']
            paused = initial[0]
            active = next(p for p in entry['promos'] if p != paused)
            config = {'membership': m, 'switches': {paused: False, active: True}}
            console._RESOLVED['config'] = config
            kept, decisions = promo_caps.filter_for_promo(
                m, active, [jid], day=DAY, disabled={paused})
            self.assertEqual(kept, [jid])
            expected = decisions[jid]['allowed']
            self.assertEqual(expected, [active])
            self.assertEqual(console.group_rows(DAY)[0]['allowedToday'], expected)
            preview = console.preview(active, DAY)
            # Preview returns the campaign metadata and per-group rows.
            row = preview['groups'][0]
            self.assertTrue(row['sendsToday'])
            self.assertEqual(row['allowedToday'], expected)
            self.assertFalse(console.preview(paused, DAY)['groups'][0]['sendsToday'])
            with patch.object(promo_config, 'resolve', return_value=config):
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(listing.main(['--json', '--date', DAY.isoformat()]), 0)
                self.assertEqual(json.loads(out.getvalue())['allowed_today'], expected)


class MalformedMembership(Harness):
    def test_bad_file_membership_still_sends_without_rereading(self):
        path = Path(self.dir) / 'membership.json'
        original = promo_membership.load
        for raw in ('{', '{"groups": []}'):
            path.write_text(raw)
            for mode in ('file', 'shadow'):
                for caps in (None, 'default'):
                    with self.subTest(raw=raw, mode=mode, caps=caps):
                        self.sent.clear()
                        self.env = {'GENAIE_CONFIG_SOURCE': mode}
                        resolver = self.resolver()

                        def resolve(promo, **kw):
                            kw['membership_path'] = str(path)
                            return resolver(promo, **kw)

                        # The harness uses caps=None. Exercise the normal caps path too.
                        import promo_broadcast as pb
                        broadcast = pb.broadcast

                        def run(**kw):
                            kw['resolve_config'] = resolve
                            if caps == 'default':
                                kw.pop('caps')
                            return broadcast(**kw)

                        with patch.object(promo_membership, 'load', wraps=original) as reads, \
                                patch.object(pb, 'broadcast', side_effect=run):
                            rc, _, err = self.run_broadcast(
                                state=str(Path(self.dir) / ('state-%s-%s-%s.json' %
                                          (len(raw), mode, caps))))
                        self.assertEqual(rc, 0, err)
                        self.assertTrue(self.sent)
                        self.assertIn('WARN: per-group caps not applied', err)
                        self.assertEqual(reads.call_count, 1)


if __name__ == '__main__':
    unittest.main()
