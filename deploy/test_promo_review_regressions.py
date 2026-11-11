"""Regression coverage for cap predictions across the console, the listing and
the engine.

A paused campaign still holds its assignments on every group it was configured
for, and a group's daily allowance has to be handed to a campaign that will
actually post. The three places that answer "what goes out today" -- the engine,
``console-read`` and ``list-groups`` -- must agree, which is what this pins.
"""
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
for path in (str(ROOT / 'workspace/lib'), str(ROOT / 'deploy')):
    if path not in sys.path:
        sys.path.insert(0, path)
from promo_fixture import DAY, membership
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
        m = copy.deepcopy(membership())
        jid = next(j for j, e in m['groups'].items()
                   if promo_membership.status_of(e) == 'active')
        entry = m['groups'][jid]
        m['groups'] = {jid: entry}
        # One group, one daily ad, two campaigns configured for it: exactly the
        # contention the reallocation has to resolve.
        entry.update(promos=['alpha', 'beta'], daily_cap=1, weekly={})
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


if __name__ == '__main__':
    unittest.main()
