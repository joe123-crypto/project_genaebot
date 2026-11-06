"""The HTTP service keeps one bounded bridge process, not one gateway per poll."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("account_controls", Path(__file__).parent / "railway/console_accounts.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AccountControlsTests(unittest.TestCase):
    def test_reuses_process_and_serializes_conflicting_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "bridge.mjs"
            script.write_text('''import { createInterface } from 'node:readline';
for await (const line of createInterface({input:process.stdin})) {
  process.stdout.write(JSON.stringify({ok:true,payload:{pid:process.pid,action:JSON.parse(line).action}})+'\\n');
}
''')
            bridge = MODULE.AccountControls(str(script))
            self.addCleanup(bridge.close)
            first_status, first = bridge.run({"action": "list"})
            second_status, second = bridge.run({"action": "list"})
            self.assertEqual((first_status, second_status), (200, 200))
            self.assertEqual(first, second)
            bridge.lock.acquire()
            try:
                self.assertEqual(bridge.run({"action": "pause"})[0], 409)
            finally:
                bridge.lock.release()

    def test_invalid_output_is_curated_and_process_is_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "bridge.mjs"
            script.write_text("process.stdin.once('data',()=>process.stdout.write('private invalid output\\n'))")
            bridge = MODULE.AccountControls(str(script))
            self.addCleanup(bridge.close)
            status, body = bridge.run({"action": "list"})
            self.assertEqual(status, 503)
            self.assertNotIn(b"private", body)
            self.assertIsNone(bridge.process)


if __name__ == "__main__":
    unittest.main()
