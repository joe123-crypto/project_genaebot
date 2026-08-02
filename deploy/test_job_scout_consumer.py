#!/usr/bin/env python3
"""Behavioral tests for deploy/job_scout_consumer.py.

Unlike the string-match assertion in test_job_scout_release.py, these build a
real extracted bundle overlay and run the consumer end to end: they prove it
accepts a valid release (including one carrying Docker init-layer siblings) and
rejects tampering. This is the gate that would have caught the missing consumer.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import job_scout_consumer as consumer  # noqa: E402

COMMIT = "a" * 40
BROWSER_DIGEST = "sha256:" + "1" * 64
LISTENER_DIGEST = "sha256:" + "2" * 64

# Every file a valid overlay must declare: the 9 required sources/locks plus the
# 4 executable live-path wrappers. Value = (relative content marker, mode).
OVERLAY_FILES: dict[str, str] = {
    "workspace/job-scout/job_scout.py": "0644",
    "workspace/job-scout/package.json": "0644",
    "workspace/job-scout/package-lock.json": "0644",
    "workspace/job-scout/channel-listener/package.json": "0644",
    "workspace/job-scout/channel-listener/package-lock.json": "0644",
    "workspace/job-scout/web_apply_server.mjs": "0644",
    "workspace/job-scout/channel-listener/listener.mjs": "0644",
    "workspace/skills/job-scout/SKILL.md": "0644",
    "workspace/docs/job-scout-onboarding.md": "0644",
    "workspace/bin/job-scout": "0755",
    "workspace/bin/job-scout-agent": "0755",
    "workspace/bin/job-scout-channel-health": "0755",
    "workspace/bin/job-scout-convert-pending": "0755",
}


def _canonical(manifest: dict) -> bytes:
    return (json.dumps(manifest, sort_keys=True, separators=(",", ":"), indent=2)
            + "\n").encode("utf-8")


def build_valid_bundle(root: Path, *, with_docker_init: bool = True) -> Path:
    """Create a schema-valid extracted bundle overlay under ``root``."""
    bundle = root / "bundle"
    bundle.mkdir()
    entries = []
    for rel, mode in OVERLAY_FILES.items():
        data = f"// {rel}\n".encode("utf-8")
        dst = bundle / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        os.chmod(dst, 0o755 if mode == "0755" else 0o644)
        entries.append({
            "path": rel,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "mode": mode,
            "executable": mode == "0755",
        })
    entries.sort(key=lambda e: e["path"])
    manifest = {
        "schemaVersion": 1,
        "repository": consumer.REPOSITORY_URL,
        "commit": COMMIT,
        "protocolVersion": 1,
        "requiredLivePaths": sorted(consumer.REQUIRED_LIVE_PATHS),
        "overlayRoot": "workspace",
        "paths": entries,
        "images": {
            "browser": {
                "name": "browser",
                "digest": BROWSER_DIGEST,
                "reference": f"{consumer.REGISTRY_ROOT}/browser@{BROWSER_DIGEST}",
            },
            "channel-listener": {
                "name": "channel-listener",
                "digest": LISTENER_DIGEST,
                "reference": f"{consumer.REGISTRY_ROOT}/channel-listener@{LISTENER_DIGEST}",
            },
        },
        "metadata": {
            "packagerVersion": "1",
            "fileCount": len(entries),
            "totalBytes": sum(e["bytes"] for e in entries),
        },
    }
    (bundle / consumer.MANIFEST_NAME).write_bytes(_canonical(manifest))
    if with_docker_init:
        # docker export + safe-extract leaves these root siblings; they are not
        # part of the release and must be tolerated.
        (bundle / "dev").mkdir()
        (bundle / "proc").mkdir()
        (bundle / "sys").mkdir()
        (bundle / "etc").mkdir()
        (bundle / "etc" / "hostname").write_text("container\n")
    return bundle


class ConsumerAcceptTests(unittest.TestCase):
    def test_valid_bundle_with_docker_init_siblings_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_valid_bundle(Path(tmp), with_docker_init=True)
            self.assertEqual(consumer.main(["validate", str(bundle)]), 0)

    def test_valid_bundle_without_siblings_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_valid_bundle(Path(tmp), with_docker_init=False)
            self.assertEqual(consumer.main(["validate", str(bundle)]), 0)


class ConsumerRejectTests(unittest.TestCase):
    def _reject(self, bundle: Path) -> None:
        self.assertEqual(consumer.main(["validate", str(bundle)]), 1)

    def test_hash_mismatch_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_valid_bundle(Path(tmp))
            (bundle / "workspace/job-scout/job_scout.py").write_bytes(b"tampered\n")
            self._reject(bundle)

    def test_mode_mismatch_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_valid_bundle(Path(tmp))
            os.chmod(bundle / "workspace/bin/job-scout", 0o644)
            self._reject(bundle)

    def test_extra_undeclared_file_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_valid_bundle(Path(tmp))
            (bundle / "workspace/job-scout/extra.py").write_text("x\n")
            self._reject(bundle)

    def test_declared_file_missing_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_valid_bundle(Path(tmp))
            (bundle / "workspace/docs/job-scout-onboarding.md").unlink()
            self._reject(bundle)

    def test_symlink_in_overlay_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_valid_bundle(Path(tmp))
            (bundle / "workspace/job-scout/evil").symlink_to("/etc/passwd")
            self._reject(bundle)

    def test_workspace_symlink_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_valid_bundle(Path(tmp), with_docker_init=False)
            ws = bundle / "workspace"
            elsewhere = Path(tmp) / "elsewhere"
            elsewhere.mkdir()
            import shutil
            shutil.rmtree(ws)
            ws.symlink_to(elsewhere)
            self._reject(bundle)

    def test_missing_manifest_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_valid_bundle(Path(tmp))
            (bundle / consumer.MANIFEST_NAME).unlink()
            self._reject(bundle)

    def test_noncanonical_manifest_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_valid_bundle(Path(tmp))
            path = bundle / consumer.MANIFEST_NAME
            manifest = json.loads(path.read_bytes())
            path.write_bytes(json.dumps(manifest).encode("utf-8"))  # compact, non-canonical
            self._reject(bundle)

    def test_tampered_metadata_totals_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_valid_bundle(Path(tmp))
            path = bundle / consumer.MANIFEST_NAME
            manifest = json.loads(path.read_bytes())
            manifest["metadata"]["totalBytes"] += 1
            path.write_bytes(_canonical(manifest))
            self._reject(bundle)

    def test_bad_image_reference_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_valid_bundle(Path(tmp))
            path = bundle / consumer.MANIFEST_NAME
            manifest = json.loads(path.read_bytes())
            manifest["images"]["browser"]["reference"] = "ghcr.io/evil/browser@" + BROWSER_DIGEST
            path.write_bytes(_canonical(manifest))
            self._reject(bundle)

    def test_bundle_not_a_directory_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._reject(Path(tmp) / "does-not-exist")


class ConsumerUnitTests(unittest.TestCase):
    def test_safe_path_rejects_traversal_and_absolute(self) -> None:
        self.assertFalse(consumer._safe_path("workspace/../etc/passwd"))
        self.assertFalse(consumer._safe_path("/etc/passwd"))
        self.assertFalse(consumer._safe_path("etc/passwd"))  # outside overlay root
        self.assertTrue(consumer._safe_path("workspace/job-scout/job_scout.py"))

    def test_looks_runtime_flags_private_state(self) -> None:
        self.assertTrue(consumer._looks_runtime("workspace/job-scout/state.json"))
        self.assertTrue(consumer._looks_runtime("workspace/job-scout/__pycache__/x.pyc"))
        self.assertTrue(consumer._looks_runtime("workspace/creds.key"))
        self.assertFalse(consumer._looks_runtime("workspace/job-scout/job_scout.py"))


if __name__ == "__main__":
    unittest.main()
