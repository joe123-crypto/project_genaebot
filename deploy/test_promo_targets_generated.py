"""The promo targets.json files are GENERATED from
workspace/cron/membership.json by workspace/bin/make-promo-targets.

This is the drift gate: if someone hand-edits a targets file, or edits
membership.json without regenerating, CI fails here and names the file. Without
it the generated files and their source would silently diverge and the single
source of truth would stop being one.

unittest-style so deploy/ci-check.sh, which runs each test file with plain
``python3``, executes them.
"""

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = ROOT / "workspace/lib"
CRON = ROOT / "workspace/cron"
GENERATOR = ROOT / "workspace/bin/make-promo-targets"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import promo_membership as pm  # noqa: E402


class GeneratedTargetsTests(unittest.TestCase):
    def setUp(self):
        self.m = pm.load(str(pm.default_path(str(ROOT / "workspace"))))
        self.assertIsNotNone(self.m)

    def test_every_committed_targets_file_matches_membership(self):
        for promo in self.m["promos"]:
            path = pm.targets_path(self.m, promo, str(CRON))
            with self.subTest(promo=promo):
                self.assertTrue(os.path.exists(path), path)
                with open(path, encoding="utf-8") as f:
                    current = f.read()
                expected = pm.dumps_targets(pm.targets_payload(self.m, promo))
                self.assertEqual(
                    current, expected,
                    "%s is out of date: run workspace/bin/make-promo-targets "
                    "--write and commit the result" % os.path.relpath(path, ROOT))

    def test_generator_reports_no_drift(self):
        proc = subprocess.run([sys.executable, str(GENERATOR), "--check"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0,
                         "make-promo-targets --check failed:\n%s%s"
                         % (proc.stdout, proc.stderr))

    def test_generator_is_executable(self):
        self.assertTrue(os.access(GENERATOR, os.X_OK), GENERATOR)

    def test_generator_refuses_an_unknown_flag(self):
        proc = subprocess.run([sys.executable, str(GENERATOR), "--oops"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)

    def test_generator_check_does_not_write(self):
        before = {}
        for promo in self.m["promos"]:
            path = pm.targets_path(self.m, promo, str(CRON))
            before[path] = (os.stat(path).st_mtime_ns, Path(path).read_bytes())
        subprocess.run([sys.executable, str(GENERATOR), "--check"],
                       capture_output=True, text=True)
        for path, (mtime, body) in before.items():
            self.assertEqual(os.stat(path).st_mtime_ns, mtime, path)
            self.assertEqual(Path(path).read_bytes(), body, path)

    def test_generated_files_declare_themselves_generated(self):
        """So nobody hand-edits one without being told where it comes from."""
        for promo in self.m["promos"]:
            path = pm.targets_path(self.m, promo, str(CRON))
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            with self.subTest(promo=promo):
                self.assertIn("GENERATED", data.get("_comment", ""))
                self.assertIn("make-promo-targets", data["_comment"])

    def test_targets_files_still_parse_through_the_engine_loaders(self):
        """The generated shape must be exactly what the engine already reads."""
        import promo_broadcast as pb

        for promo in self.m["promos"]:
            path = pm.targets_path(self.m, promo, str(CRON))
            jids, weekly = pm.groups_for_promo(self.m, promo)
            with self.subTest(promo=promo):
                self.assertEqual(pb.load_targets(path), jids)
                loaded = pb.load_weekly_targets(path)
                self.assertEqual(sorted(loaded), sorted(weekly))

    def test_every_generated_file_lists_valid_unique_group_jids(self):
        """The shape check deploy/test_<promo>_promo_targets.py makes per promo,
        made once for all seven -- fruit-trees has no file of its own, and a
        generated file is the wrong place to start writing more of them."""
        group_jid = re.compile(r"^\d+(-\d+)?@g\.us$")
        for promo in self.m["promos"]:
            path = pm.targets_path(self.m, promo, str(CRON))
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            with self.subTest(promo=promo):
                groups = data["groups"]
                self.assertIsInstance(groups, list)
                self.assertGreater(len(groups), 0, "%s has no targets" % promo)
                for entry in list(groups) + list(data.get("weekly_groups") or {}):
                    self.assertRegex(entry, group_jid, promo)
                self.assertEqual(sorted(set(groups)), sorted(groups),
                                 "%s has duplicate targets" % promo)

    def test_no_stray_targets_file_without_a_promo_entry(self):
        on_disk = sorted(p.name[: -len("-promo")] for p in CRON.glob("*-promo")
                         if (p / "targets.json").exists())
        self.assertEqual(sorted(self.m["promos"]), on_disk)


if __name__ == "__main__":
    unittest.main()
