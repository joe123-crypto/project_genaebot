from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from job_scout_cutover import (  # noqa: E402
    EXPECTED_CRONS,
    ValidationError,
    assert_runtime_unchanged,
    compare_overlay,
    file_record,
    record_evidence,
    reject_runtime_path,
    runtime_snapshot,
    validate_rollback,
    write_json_atomic,
)


REPOSITORY = "https://github.com/joe123-crypto/genaie-job-scout"
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
DIGEST_A = "sha256:" + "1" * 64
DIGEST_B = "sha256:" + "2" * 64
DIGEST_C = "sha256:" + "3" * 64
WRAPPERS = [
    "workspace/bin/job-scout",
    "workspace/bin/job-scout-agent",
    "workspace/bin/job-scout-channel-health",
    "workspace/bin/job-scout-convert-pending",
]
REQUIRED_LIVE_PATHS = sorted(f"/home/joseph/.openclaw/{path}" for path in WRAPPERS)
REQUIRED_FILES = [
    *WRAPPERS,
    "workspace/docs/job-scout-onboarding.md",
    "workspace/job-scout/channel-listener/listener.mjs",
    "workspace/job-scout/channel-listener/package-lock.json",
    "workspace/job-scout/channel-listener/package.json",
    "workspace/job-scout/job_scout.py",
    "workspace/job-scout/package-lock.json",
    "workspace/job-scout/package.json",
    "workspace/job-scout/web_apply_server.mjs",
    "workspace/skills/job-scout/SKILL.md",
]
CONTRACT_REQUIRED_FILES = [path for path in REQUIRED_FILES if path not in WRAPPERS]


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.curated = root / "curated"
        self.bundle = root / "bundle"
        self.allowlist = root / "allowlist.json"
        self.manifest = self.bundle / "job-scout-manifest.json"
        self.curated.mkdir()
        self.bundle.mkdir()
        self.allowlist.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "manifestContractCommit": "9459a9b",
                    "repository": REPOSITORY,
                    "protocolVersion": 1,
                    "overlayRoot": "workspace",
                    "trees": ["workspace/job-scout"],
                    "files": [
                        *WRAPPERS,
                        "workspace/docs/job-scout-onboarding.md",
                        "workspace/skills/job-scout/SKILL.md",
                    ],
                    "requiredFiles": CONTRACT_REQUIRED_FILES,
                    "requiredLivePaths": REQUIRED_LIVE_PATHS,
                    "sourceRemovalPaths": [
                        "deploy/job-scout-browser.Dockerfile",
                        "deploy/job-scout-channel-listener.Dockerfile",
                        "workspace/job-scout",
                        *WRAPPERS,
                        "workspace/docs/job-scout-onboarding.md",
                        "workspace/skills/job-scout",
                    ],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        for index, rel in enumerate(REQUIRED_FILES):
            mode = 0o755 if rel in WRAPPERS else 0o644
            data = f"fixture-{index}-{rel}\n".encode()
            self.write_file(self.curated, rel, data, mode)
            self.write_file(self.bundle, rel, data, mode)
        self.write_manifest(COMMIT_A)

    @staticmethod
    def write_file(root: Path, rel: str, data: bytes, mode: int) -> None:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)

    def manifest_payload(self, commit: str = COMMIT_A) -> dict:
        paths = [file_record(self.bundle, rel, "fixture") for rel in sorted(REQUIRED_FILES)]
        return {
            "schemaVersion": 1,
            "repository": REPOSITORY,
            "commit": commit,
            "protocolVersion": 1,
            "requiredLivePaths": REQUIRED_LIVE_PATHS,
            "overlayRoot": "workspace",
            "paths": paths,
            "images": {
                "browser": {
                    "name": "browser",
                    "digest": DIGEST_B,
                    "reference": f"ghcr.io/joe123-crypto/genaie-job-scout/browser@{DIGEST_B}",
                },
                "channel-listener": {
                    "name": "channel-listener",
                    "digest": DIGEST_C,
                    "reference": f"ghcr.io/joe123-crypto/genaie-job-scout/channel-listener@{DIGEST_C}",
                },
            },
            "metadata": {
                "packagerVersion": "1",
                "fileCount": len(paths),
                "totalBytes": sum(entry["bytes"] for entry in paths),
            },
        }

    def write_manifest(self, commit: str = COMMIT_A, mutate=None) -> dict:
        payload = self.manifest_payload(commit)
        if mutate is not None:
            mutate(payload)
        self.manifest.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        self.manifest.chmod(0o644)
        return payload


def cron_snapshot(path: Path) -> None:
    jobs = []
    for index, (name, expected) in enumerate(EXPECTED_CRONS.items(), start=1):
        if "argv" in expected:
            argv = expected["argv"]
        else:
            argv = [*expected["argvPrefix"], "120000000000@g.us", *expected["argvSuffix"]]
        jobs.append(
            {
                "id": f"cron-{index}",
                "name": name,
                "enabled": True,
                "schedule": {"expr": expected["expr"], "tz": expected["tz"], "kind": "cron"},
                "sessionTarget": "isolated",
                "payload": {
                    "kind": "command",
                    "argv": argv,
                    "cwd": "/home/joseph/.openclaw/workspace",
                    "timeoutSeconds": expected["timeout"],
                },
                "failureAlert": {
                    "after": 1,
                    "channel": "whatsapp",
                    "to": "+0000000000",
                    "cooldown": "1h",
                },
            }
        )
    jobs.append({"id": "unrelated", "name": "event-scout", "enabled": True})
    path.write_text(json.dumps({"jobs": jobs}) + "\n", encoding="utf-8")


class OverlayParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = Fixture(Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def compare(self) -> dict:
        return compare_overlay(
            self.fixture.bundle,
            self.fixture.curated,
            self.fixture.manifest,
            self.fixture.allowlist,
        )

    def test_exact_ws3_overlay_passes(self) -> None:
        result = self.compare()
        self.assertEqual(result["result"], "pass")
        self.assertEqual(result["fileCount"], len(REQUIRED_FILES))
        self.assertIn("mode", result["checks"])

    def test_rejects_ws4_files_integer_modes_and_string_images_shape(self) -> None:
        def mutate(payload: dict) -> None:
            payload["files"] = [
                {**entry, "mode": int(entry["mode"], 8)} for entry in payload.pop("paths")
            ]
            payload["images"] = {
                "browser": DIGEST_B,
                "channel-listener": DIGEST_C,
            }

        self.fixture.write_manifest(mutate=mutate)
        with self.assertRaisesRegex(ValidationError, "manifest paths"):
            self.compare()

    def test_rejects_integer_mode_in_paths(self) -> None:
        self.fixture.write_manifest(
            mutate=lambda payload: payload["paths"][0].update(mode=0o644)
        )
        with self.assertRaisesRegex(ValidationError, "invalid mode"):
            self.compare()

    def test_rejects_unstructured_images(self) -> None:
        self.fixture.write_manifest(
            mutate=lambda payload: payload.update(
                images={"browser": DIGEST_B, "channel-listener": DIGEST_C}
            )
        )
        with self.assertRaisesRegex(ValidationError, "must be structured"):
            self.compare()

    def test_rejects_hash_byte_and_mode_mismatches(self) -> None:
        target = self.fixture.bundle / REQUIRED_FILES[-1]
        target.write_bytes(target.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValidationError, "bundle metadata differs"):
            self.compare()

        self.fixture.write_manifest()
        target.chmod(0o755)
        with self.assertRaisesRegex(ValidationError, "bundle metadata differs"):
            self.compare()

    def test_rejects_actual_on_disk_0600_0700_and_0777_mode_drift(self) -> None:
        for side, root in (("bundle", self.fixture.bundle), ("curated", self.fixture.curated)):
            regular = root / "workspace/job-scout/job_scout.py"
            executable = root / "workspace/bin/job-scout"
            for path, drifted_mode in (
                (regular, 0o600),
                (executable, 0o700),
                (executable, 0o777),
            ):
                with self.subTest(side=side, mode=f"{drifted_mode:04o}"):
                    original_mode = path.stat().st_mode & 0o7777
                    path.chmod(drifted_mode)
                    try:
                        with self.assertRaisesRegex(
                            ValidationError,
                            f"unsupported on-disk mode {drifted_mode:04o}",
                        ):
                            self.compare()
                    finally:
                        path.chmod(original_mode)

    def test_rejects_extra_files_and_directories(self) -> None:
        self.fixture.write_file(self.fixture.bundle, "workspace/extra.txt", b"extra", 0o644)
        with self.assertRaisesRegex(ValidationError, "extras"):
            self.compare()
        (self.fixture.bundle / "workspace/extra.txt").unlink()
        (self.fixture.bundle / "workspace/empty").mkdir()
        with self.assertRaisesRegex(ValidationError, "undeclared directories"):
            self.compare()

    def test_rejects_symlinks_and_unsafe_paths(self) -> None:
        link = self.fixture.bundle / "workspace/job-scout/link"
        link.symlink_to("job_scout.py")
        with self.assertRaisesRegex(ValidationError, "symlink"):
            self.compare()
        link.unlink()

        def mutate(payload: dict) -> None:
            payload["paths"][0]["path"] = "workspace/../escape"

        self.fixture.write_manifest(mutate=mutate)
        with self.assertRaisesRegex(ValidationError, "unsafe|non-canonical"):
            self.compare()

    def test_rejects_runtime_paths_even_inside_owned_tree(self) -> None:
        rel = "workspace/job-scout/runtime/state.json"
        self.fixture.write_file(self.fixture.bundle, rel, b"private", 0o644)

        def mutate(payload: dict) -> None:
            payload["paths"].append(file_record(self.fixture.bundle, rel, "fixture"))
            payload["paths"].sort(key=lambda entry: entry["path"])
            payload["metadata"]["fileCount"] += 1
            payload["metadata"]["totalBytes"] += len(b"private")

        self.fixture.write_manifest(mutate=mutate)
        with self.assertRaisesRegex(ValidationError, "runtime/private/cache"):
            self.compare()

    def test_rejects_ws3_credential_session_and_key_denylist(self) -> None:
        for rel in (
            "workspace/job-scout/creds.json",
            "workspace/job-scout/token.creds",
            "workspace/job-scout/browser.session",
            "workspace/job-scout/private.key",
            "workspace/job-scout/certificate.pem",
            "workspace/job-scout/.env.local",
        ):
            with self.subTest(path=rel):
                with self.assertRaisesRegex(ValidationError, "runtime/private/cache"):
                    reject_runtime_path(rel)

    def test_rejects_curated_file_missing_from_manifest(self) -> None:
        self.fixture.write_file(
            self.fixture.curated,
            "workspace/job-scout/new_source.py",
            b"new\n",
            0o644,
        )
        with self.assertRaisesRegex(ValidationError, "missing=.*new_source"):
            self.compare()


class EvidenceAndRollbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = Fixture(self.root)
        self.runtime = self.root / "runtime-root"
        (self.runtime / "job-scout-channel/auth").mkdir(parents=True)
        (self.runtime / "job-scout/state.db").parent.mkdir(parents=True)
        (self.runtime / "job-scout/state.db").write_bytes(b"runtime-state")
        (self.runtime / "job-scout-channel/auth/creds.json").write_bytes(b"runtime-auth")
        self.before_path = self.root / "runtime-before.json"
        self.after_path = self.root / "runtime-after.json"
        before = runtime_snapshot(self.runtime)
        after = runtime_snapshot(self.runtime)
        before["capturedAt"] = "2026-08-01T09:59:00Z"
        after["capturedAt"] = "2026-08-01T10:01:00Z"
        write_json_atomic(self.before_path, before)
        write_json_atomic(self.after_path, after)
        self.crons = self.root / "crons.json"
        cron_snapshot(self.crons)
        self.records = self.root / "records"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def record_args(self, commit: str, status: str = "production-success") -> argparse.Namespace:
        return argparse.Namespace(
            allowlist=self.fixture.allowlist,
            bundle_root=self.fixture.bundle,
            curated_root=self.fixture.curated,
            manifest=self.fixture.manifest,
            runtime_before=self.before_path,
            runtime_after=self.after_path,
            cron_snapshot=self.crons,
            records_dir=self.records,
            genaebot_sha="c" * 40,
            job_scout_sha=commit,
            source_bundle_digest=DIGEST_A,
            browser_digest=DIGEST_B,
            listener_digest=DIGEST_C,
            deployment_time="2026-08-01T10:00:00Z",
            job_scout_workflow_run_id="101",
            genaebot_workflow_run_id="202",
            correlation_id=f"correlation-{commit[:4]}",
            deployment_status=status,
        )

    def test_runtime_snapshot_detects_change(self) -> None:
        before = runtime_snapshot(self.runtime)
        (self.runtime / "job-scout/state.db").write_bytes(b"changed")
        after = runtime_snapshot(self.runtime)
        with self.assertRaisesRegex(ValidationError, "runtime changed"):
            assert_runtime_unchanged(before, after)

    def test_runtime_snapshot_digest_is_deterministic(self) -> None:
        first = runtime_snapshot(self.runtime)
        second = runtime_snapshot(self.runtime)
        self.assertEqual(first["digest"], second["digest"])

    def test_record_captures_composite_five_crons_and_preserves_runtime(self) -> None:
        state_before = (self.runtime / "job-scout/state.db").read_bytes()
        evidence = record_evidence(self.record_args(COMMIT_A))
        self.assertEqual(evidence["compositeRevision"]["genaebotCommit"], "c" * 40)
        self.assertEqual(evidence["compositeRevision"]["jobScoutCommit"], COMMIT_A)
        self.assertEqual(evidence["compositeRevision"]["browserDigest"], DIGEST_B)
        self.assertEqual(len(evidence["cronSchedules"]), 5)
        self.assertTrue(all(job["enabled"] for job in evidence["cronSchedules"]))
        self.assertTrue(evidence["runtime"]["unchanged"])
        self.assertEqual((self.runtime / "job-scout/state.db").read_bytes(), state_before)
        self.assertTrue((self.records / "record-000001-aaaaaaaaaaaa/overlay").is_dir())

    def test_record_rejects_manifest_image_digest_mismatch(self) -> None:
        args = self.record_args(COMMIT_A)
        args.browser_digest = DIGEST_A
        with self.assertRaisesRegex(ValidationError, "browser composite digest"):
            record_evidence(args)

    def test_record_rejects_missing_or_changed_cron(self) -> None:
        payload = json.loads(self.crons.read_text(encoding="utf-8"))
        payload["jobs"][0]["enabled"] = False
        self.crons.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "must be enabled"):
            record_evidence(self.record_args(COMMIT_A))

    def test_record_rejects_failure_alert_and_payload_shape_drift(self) -> None:
        original = json.loads(self.crons.read_text(encoding="utf-8"))
        mutations = (
            ("alert-channel", lambda job: job["failureAlert"].update(channel="email"), "failure alert"),
            ("alert-recipient", lambda job: job["failureAlert"].update(to=""), "failure alert"),
            ("alert-cooldown", lambda job: job["failureAlert"].update(cooldown="2h"), "failure alert"),
            ("payload-kind", lambda job: job["payload"].update(kind="agentTurn"), "payload kind"),
            ("payload-cwd", lambda job: job["payload"].update(cwd="/tmp"), "working directory"),
            ("payload-timeout", lambda job: job["payload"].update(timeoutSeconds=1), "timeout"),
        )
        for label, mutate, message in mutations:
            with self.subTest(field=label):
                payload = deepcopy(original)
                mutate(payload["jobs"][0])
                self.crons.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(ValidationError, message):
                    record_evidence(self.record_args(COMMIT_A))
        self.crons.write_text(json.dumps(original), encoding="utf-8")

    def test_record_rejects_same_runtime_snapshot_file(self) -> None:
        args = self.record_args(COMMIT_A)
        args.runtime_after = args.runtime_before
        with self.assertRaisesRegex(ValidationError, "must be distinct files"):
            record_evidence(args)

    def test_record_rejects_runtime_capture_times_outside_deployment_window(self) -> None:
        before = json.loads(self.before_path.read_text(encoding="utf-8"))
        after = json.loads(self.after_path.read_text(encoding="utf-8"))
        before["capturedAt"] = "2026-08-01T10:01:00Z"
        after["capturedAt"] = "2026-08-01T10:02:00Z"
        write_json_atomic(self.before_path, before)
        write_json_atomic(self.after_path, after)
        with self.assertRaisesRegex(ValidationError, "must bracket"):
            record_evidence(self.record_args(COMMIT_A))

    def test_record_rejects_copied_runtime_snapshot_timestamp(self) -> None:
        before = json.loads(self.before_path.read_text(encoding="utf-8"))
        after = json.loads(self.after_path.read_text(encoding="utf-8"))
        after["capturedAt"] = before["capturedAt"]
        write_json_atomic(self.after_path, after)
        with self.assertRaisesRegex(ValidationError, "strictly ordered"):
            record_evidence(self.record_args(COMMIT_A))

    def test_rollback_only_accepts_an_earlier_cataloged_success_manifest(self) -> None:
        first = record_evidence(self.record_args(COMMIT_A))
        self.fixture.write_manifest(COMMIT_B)
        second = record_evidence(self.record_args(COMMIT_B))
        first_manifest = self.records / first["recordId"] / "release-manifest.json"
        args = argparse.Namespace(
            records_dir=self.records,
            manifest=first_manifest,
            current_record_id=second["recordId"],
            allowlist=self.fixture.allowlist,
        )
        selection = validate_rollback(args)
        self.assertEqual(selection["selectedRecordId"], first["recordId"])
        self.assertEqual(selection["runtimePolicy"], "preserve")

        arbitrary = self.root / "arbitrary-manifest.json"
        arbitrary.write_bytes(first_manifest.read_bytes())
        args.manifest = arbitrary
        with self.assertRaisesRegex(ValidationError, "not a previously recorded"):
            validate_rollback(args)

        args.manifest = self.records / second["recordId"] / "release-manifest.json"
        with self.assertRaisesRegex(ValidationError, "must precede"):
            validate_rollback(args)

    def test_local_rehearsal_record_is_not_rollback_eligible(self) -> None:
        local = record_evidence(self.record_args(COMMIT_A, "local-rehearsal"))
        self.fixture.write_manifest(COMMIT_B)
        current = record_evidence(self.record_args(COMMIT_B))
        args = argparse.Namespace(
            records_dir=self.records,
            manifest=self.records / local["recordId"] / "release-manifest.json",
            current_record_id=current["recordId"],
            allowlist=self.fixture.allowlist,
        )
        with self.assertRaisesRegex(ValidationError, "not from a successful production"):
            validate_rollback(args)


if __name__ == "__main__":
    unittest.main()
