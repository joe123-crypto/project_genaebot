"""Exercise retirement through the same CLI boundary used against production."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
FEEDS = ("vacancy-lamp-feed", "hustlers-feed", "job-scout-group-feed", "job-scout-hustlers-feed")


class RetirementTests(unittest.TestCase):
    def test_current_legacy_duplicate_names_and_repeat_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            jobs = [{"id": str(i), "name": name} for i, name in enumerate(
                (*FEEDS, "vacancy-lamp-feed", "cv-promo", "heartbeat-main"))]
            state = root / "jobs.json"
            state.write_text(json.dumps({"jobs": jobs}))
            railway = root / "railway"
            railway.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
p = pathlib.Path(os.environ["FIXTURE_JOBS"])
data = json.loads(p.read_text())
args = sys.argv[sys.argv.index("cron") + 1:]
if args[:1] == ["list"]:
    print(json.dumps(data))
elif args[:1] == ["remove"]:
    data["jobs"] = [j for j in data["jobs"] if j["id"] != args[1]]
    p.write_text(json.dumps(data))
    print("{}")
else:
    raise SystemExit(2)
''')
            railway.chmod(0o755)
            env = dict(os.environ, OPENCLAW_EXEC_MODE="railway", RAILWAY=str(railway),
                       FIXTURE_JOBS=str(state))
            def run(*args):
                return subprocess.run(["bash", str(ROOT / "deploy/retire-job-scout-crons.sh"), *args],
                                      env=env, text=True, capture_output=True, check=True).stdout
            dry = run("--dry-run")
            self.assertEqual(dry.count("would remove"), 5)
            self.assertEqual(json.loads(state.read_text())["jobs"], jobs)
            self.assertEqual(run().count("removed "), 5)
            self.assertEqual([j["name"] for j in json.loads(state.read_text())["jobs"]],
                             ["cv-promo", "heartbeat-main"])
            self.assertIn("no retired crons found", run())


if __name__ == "__main__":
    unittest.main()
