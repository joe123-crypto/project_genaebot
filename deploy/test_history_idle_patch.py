import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("history_patch", ROOT / "deploy/railway/patch-history-idle.py")
PATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCH)


class HistoryIdlePatchTests(unittest.TestCase):
    def source(self):
        return '\n'.join([
            'function armDatabaseWorkerIdleRetirement(lane)',
            'lane.nativeSequence <= lane.retiredSequence || lane.pending > 0',
            'rotateDatabaseWorkers(lane).catch',
            'lane.pool.rotate().then(() => releaseRetiredDatabaseCustody(lane, through))',
            'const historyWorkerLanes = [', PATCH.ANCHOR,
        ])

    def test_only_owner_timer_changes_and_opt_out_restores_upstream_retention(self):
        source = self.source()
        actual = PATCH.transform(source)
        self.assertEqual(actual, PATCH.MARKER + '\n' + source.replace(PATCH.ANCHOR, PATCH.REPLACEMENT))
        self.assertEqual(PATCH.transform(actual), actual)
        self.assertIn('historyWorkerLanes.includes(lane) ? 300_000 : SQLITE_IDLE_HANDLE_TTL_MS', actual)

    def test_changed_custody_contract_or_duplicate_anchor_fails_closed(self):
        for source in [self.source().replace('lane.pending > 0', 'false'),
                       self.source().replace('releaseRetiredDatabaseCustody', 'other'),
                       self.source() + PATCH.ANCHOR]:
            with self.assertRaises(ValueError):
                PATCH.transform(source)


if __name__ == '__main__':
    unittest.main()
