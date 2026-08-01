from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from job_scout_cutover import ValidationError  # noqa: E402
from job_scout_source_guard import check_source_guard  # noqa: E402


ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = Path(__file__).with_name("job-scout-owned-paths.json")
ACTIVATION = Path(__file__).with_name("job-scout-source-removal.json")


class JobScoutSourceGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.allowlist = self.root / "allowlist.json"
        shutil.copyfile(ALLOWLIST, self.allowlist)
        self.activation = self.root / "activation.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def activate(self, enabled: bool = True) -> None:
        self.activation.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "enabled": enabled,
                    "cutoverRecordId": "record-000001-aaaaaaaaaaaa",
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def test_guard_is_disabled_until_activation_exists(self) -> None:
        path = self.root / "workspace/job-scout/job_scout.py"
        path.parent.mkdir(parents=True)
        path.write_text("source\n", encoding="utf-8")
        enabled, violations = check_source_guard(self.root, self.allowlist, self.activation)
        self.assertFalse(enabled)
        self.assertEqual(violations, [])

    def test_activated_guard_rejects_reintroduced_source_and_worker_builds(self) -> None:
        self.activate()
        source = self.root / "workspace/job-scout/job_scout.py"
        source.parent.mkdir(parents=True)
        source.write_text("source\n", encoding="utf-8")
        dockerfile = self.root / "deploy/job-scout-browser.Dockerfile"
        dockerfile.parent.mkdir(parents=True)
        dockerfile.write_text("FROM scratch\n", encoding="utf-8")
        enabled, violations = check_source_guard(self.root, self.allowlist, self.activation)
        self.assertTrue(enabled)
        self.assertEqual(
            violations,
            ["deploy/job-scout-browser.Dockerfile", "workspace/job-scout"],
        )

    def test_activation_must_be_explicitly_enabled_and_reference_cutover(self) -> None:
        self.activate(enabled=False)
        with self.assertRaisesRegex(ValidationError, "enabled to true"):
            check_source_guard(self.root, self.allowlist, self.activation)
        self.activation.write_text('{"schemaVersion": 1, "enabled": true}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "cutoverRecordId"):
            check_source_guard(self.root, self.allowlist, self.activation)

    def test_repository_guard_matches_current_activation_state(self) -> None:
        enabled, violations = check_source_guard(ROOT, ALLOWLIST, ACTIVATION)
        self.assertEqual(violations, [], f"source guard enabled={enabled} violations={violations}")


if __name__ == "__main__":
    unittest.main()
