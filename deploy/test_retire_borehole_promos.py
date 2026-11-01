import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/retire-borehole-promos.sh"
SHIM = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
p=Path(os.environ['FAKE_JOBS'])
d=json.loads(p.read_text())
args=sys.argv[sys.argv.index('cron')+1:]
if args[:1]==['list']:
 print(json.dumps(d))
elif args[:1]==['remove']:
 d['jobs']=[j for j in d['jobs'] if j['id']!=args[1]]
 p.write_text(json.dumps(d))
else:
 raise SystemExit('unexpected mutation')
'''


def jobs():
    return {"jobs": [
        {"name": "borehole-drilling-promo", "id": "retained", "enabled": True,
         "schedule": {"expr": "15 7 * * *", "tz": "Africa/Harare"},
         "payload": {"argv": ["python3", "/home/joseph/.openclaw/workspace/cron/borehole-drilling-promo/post_ad.py"]}},
        {"name": "big-player-promo", "id": "old1"},
        {"name": "big-player-promo", "id": "duplicate"},
        {"name": "borehole-paid-promo", "id": "old2"},
        {"name": "skylight-solar-promo", "id": "skylight", "enabled": True},
        {"name": "unrelated-job", "id": "other"},
    ]}


class RetireBoreholeTests(unittest.TestCase):
    def run_script(self, payload, *args):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fake = directory / "gateway"
            fake.write_text(SHIM)
            fake.chmod(0o755)
            state = directory / "jobs.json"
            state.write_text(json.dumps(payload))
            result = subprocess.run(["bash", str(SCRIPT), *args], text=True, capture_output=True,
                                    env={**os.environ, "OPENCLAW_EXEC_MODE": "docker", "DOCKER": str(fake), "FAKE_JOBS": str(state)})
            return result, json.loads(state.read_text())

    def test_removes_only_retired_names_including_duplicates(self):
        initial = jobs()
        result, remaining = self.run_script(initial)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(remaining["jobs"], [initial["jobs"][i] for i in [0, 4, 5]])
        again, unchanged = self.run_script(remaining)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(remaining, unchanged)

    def test_dry_run_never_mutates(self):
        initial = jobs()
        result, remaining = self.run_script(initial, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(initial, remaining)
        self.assertIn("would remove", result.stdout)

    def test_active_affected_job_blocks_migration(self):
        for index in [0, 1, 3]:
            initial = jobs()
            initial["jobs"][index]["state"] = {"runningAtMs": 123}
            result, remaining = self.run_script(initial)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(initial, remaining)

    def test_unverified_replacement_blocks_removal(self):
        initial = jobs()
        initial["jobs"][0]["enabled"] = False
        result, remaining = self.run_script(initial)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(initial, remaining)


if __name__ == "__main__":
    unittest.main()
