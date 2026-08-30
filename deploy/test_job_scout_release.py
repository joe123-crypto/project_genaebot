from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "deploy/job_scout_release.py"
WORKFLOW_PATH = ROOT / ".github/workflows/deploy-main.yml"
SPEC = importlib.util.spec_from_file_location("job_scout_release", MODULE_PATH)
assert SPEC and SPEC.loader
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)

JOB_SCOUT_SHA = "1" * 40
GENAEBOT_SHA = "2" * 40
DIGESTS = {
    "bundleDigest": "sha256:" + "a" * 64,
    "browserDigest": "sha256:" + "b" * 64,
    "listenerDigest": "sha256:" + "c" * 64,
}


def payload(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "jobScoutSha": JOB_SCOUT_SHA,
        **DIGESTS,
        "manifestHash": "d" * 64,
        "sourceRunId": "123456789",
        "correlationId": "job-scout-12345678",
    }
    value.update(changes)
    return value


def request(**changes: object) -> dict[str, object]:
    value = {
        **payload(),
        "schemaVersion": 1,
        "deploymentMode": "release",
        "genaebotSha": GENAEBOT_SHA,
        "receiverRunId": "987654321",
        "receiverRunAttempt": "1",
    }
    value.update(changes)
    return value


def manifest(req: dict[str, object], **changes: object) -> dict[str, object]:
    refs = release.artifact_references(req)
    value: dict[str, object] = {
        "schemaVersion": 1,
        "protocolVersion": 1,
        "repository": release.JOB_SCOUT_REPOSITORY_URL,
        "commit": req["jobScoutSha"],
        "overlayRoot": "workspace",
        "requiredLivePaths": [],
        "paths": [
            {
                "path": "workspace/job-scout/job_scout.py",
                "sha256": "e" * 64,
                "bytes": 1,
                "mode": "0644",
                "executable": False,
            }
        ],
        "images": {
            "browser": {
                "name": "browser",
                "digest": req["browserDigest"],
                "reference": refs["browser_ref"],
            },
            "channel-listener": {
                "name": "channel-listener",
                "digest": req["listenerDigest"],
                "reference": refs["listener_ref"],
            },
        },
        "metadata": {"packagerVersion": "1", "fileCount": 1, "totalBytes": 1},
    }
    value.update(changes)
    return value


def write_bound_request(root: Path) -> tuple[Path, Path, dict[str, object]]:
    bundle = root / "bundle"
    bundle.mkdir()
    req = request()
    manifest_path = bundle / "job-scout-manifest.json"
    manifest_path.write_text(json.dumps(manifest(req), sort_keys=True) + "\n", encoding="utf-8")
    req["manifestHash"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    request_path = root / "job-scout-deploy-request.json"
    request_path.write_text(json.dumps(req, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return request_path, bundle, req


class DispatchPayloadTests(unittest.TestCase):
    def event(self, client_payload: object) -> dict[str, object]:
        return {
            "action": "job_scout_release",
            "repository": {"full_name": release.GENAEBOT_REPOSITORY},
            "client_payload": client_payload,
        }

    def parse(self, value: object) -> dict[str, object]:
        return release.parse_dispatch_event(
            self.event(value),
            genaebot_sha=GENAEBOT_SHA,
            receiver_run_id="987654321",
            receiver_run_attempt="1",
        )

    def test_exact_digest_only_payload_is_accepted(self) -> None:
        parsed = self.parse(payload())
        self.assertEqual(parsed["deploymentMode"], "release")
        self.assertEqual(parsed["jobScoutSha"], JOB_SCOUT_SHA)

    def test_missing_extra_and_non_string_fields_are_rejected(self) -> None:
        cases = []
        missing = payload()
        del missing["sourceRunId"]
        cases.append(missing)
        cases.append(payload(unexpected="value"))
        cases.append(payload(sourceRunId=123456789))
        for value in cases:
            with self.subTest(value=value), self.assertRaises(release.ReleaseError):
                self.parse(value)

    def test_mutable_or_reference_values_are_rejected_as_digests(self) -> None:
        for invalid in (
            "latest",
            "ghcr.io/joe123-crypto/genaie-job-scout/browser:latest",
            "ghcr.io/joe123-crypto/genaie-job-scout/browser@sha256:" + "b" * 64,
            "sha256:" + "B" * 64,
        ):
            with self.subTest(invalid=invalid), self.assertRaises(release.ReleaseError):
                self.parse(payload(browserDigest=invalid))

    def test_distinct_digests_and_safe_correlation_are_required(self) -> None:
        with self.assertRaises(release.ReleaseError):
            self.parse(payload(browserDigest=DIGESTS["bundleDigest"]))
        with self.assertRaises(release.ReleaseError):
            self.parse(payload(correlationId="$(touch /tmp/injected)"))

    def test_event_type_and_target_repository_are_bound(self) -> None:
        event = self.event(payload())
        event["action"] = "anything_else"
        with self.assertRaises(release.ReleaseError):
            release.parse_dispatch_event(
                event, genaebot_sha=GENAEBOT_SHA,
                receiver_run_id="1", receiver_run_attempt="1"
            )
        event = self.event(payload())
        event["repository"] = {"full_name": "someone/else"}
        with self.assertRaises(release.ReleaseError):
            release.parse_dispatch_event(
                event, genaebot_sha=GENAEBOT_SHA,
                receiver_run_id="1", receiver_run_attempt="1"
            )


class ManifestBindingTests(unittest.TestCase):
    def test_canonical_ws3_manifest_is_bound_to_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            request_path, bundle, req = write_bound_request(Path(tmp))
            request_hash = hashlib.sha256(request_path.read_bytes()).hexdigest()
            validated = release.verify_request_bundle(
                request_path, bundle, request_hash, GENAEBOT_SHA
            )
            self.assertEqual(validated, req)

    def test_legacy_ws4_draft_shape_is_not_silently_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request_path, bundle, req = write_bound_request(root)
            legacy = manifest(req)
            legacy["files"] = [
                {"path": "workspace/job-scout/job_scout.py", "mode": 0o644}
            ]
            del legacy["paths"]
            legacy["images"] = {
                "browser": release.artifact_references(req)["browser_ref"],
                "channelListener": release.artifact_references(req)["listener_ref"],
            }
            manifest_path = bundle / "job-scout-manifest.json"
            manifest_path.write_text(json.dumps(legacy), encoding="utf-8")
            req["manifestHash"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            release._canonical_json(request_path, req)
            with self.assertRaisesRegex(release.ReleaseError, "legacy"):
                release.verify_request_bundle(
                    request_path, bundle,
                    hashlib.sha256(request_path.read_bytes()).hexdigest(), GENAEBOT_SHA
                )

    def test_repository_commit_images_and_manifest_hash_are_all_checked(self) -> None:
        mutations = {
            "repository": {"repository": "https://github.com/attacker/repo"},
            "commit": {"commit": "9" * 40},
            "browser": {
                "images": {
                    "browser": {"reference": "ghcr.io/attacker/browser@" + DIGESTS["browserDigest"]},
                    "channel-listener": {
                        "reference": f"{release.GHCR_ROOT}/channel-listener@{DIGESTS['listenerDigest']}"
                    },
                }
            },
        }
        for name, change in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                request_path, bundle, req = write_bound_request(root)
                manifest_path = bundle / "job-scout-manifest.json"
                manifest_path.write_text(json.dumps(manifest(req, **change)), encoding="utf-8")
                req["manifestHash"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                release._canonical_json(request_path, req)
                with self.assertRaises(release.ReleaseError):
                    release.verify_request_bundle(
                        request_path, bundle,
                        hashlib.sha256(request_path.read_bytes()).hexdigest(), GENAEBOT_SHA
                    )

        with tempfile.TemporaryDirectory() as tmp:
            request_path, bundle, _ = write_bound_request(Path(tmp))
            request_hash = hashlib.sha256(request_path.read_bytes()).hexdigest()
            (bundle / "job-scout-manifest.json").write_bytes(b"{}\n")
            with self.assertRaisesRegex(release.ReleaseError, "SHA-256"):
                release.verify_request_bundle(request_path, bundle, request_hash)

    def test_request_hash_and_receiver_sha_are_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            request_path, bundle, _ = write_bound_request(Path(tmp))
            with self.assertRaisesRegex(release.ReleaseError, "request SHA-256"):
                release.verify_request_bundle(request_path, bundle, "f" * 64)
            actual = hashlib.sha256(request_path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(release.ReleaseError, "different genaebot"):
                release.verify_request_bundle(request_path, bundle, actual, "9" * 40)


class ImageProvenanceTests(unittest.TestCase):
    def test_expected_digest_and_labels_are_required(self) -> None:
        req = request()
        expected = release.artifact_references(req)["browser_ref"]
        inspect = [{
            "RepoDigests": [expected],
            "Config": {"Labels": {
                "org.opencontainers.image.source": release.JOB_SCOUT_REPOSITORY_URL,
                "org.opencontainers.image.revision": JOB_SCOUT_SHA,
                "io.genaie.jobscout.artifact": "browser",
            }},
        }]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inspect.json"
            path.write_text(json.dumps(inspect), encoding="utf-8")
            release.verify_image_inspect(path, req, "browser")
            inspect[0]["Config"]["Labels"]["org.opencontainers.image.revision"] = "9" * 40
            path.write_text(json.dumps(inspect), encoding="utf-8")
            with self.assertRaises(release.ReleaseError):
                release.verify_image_inspect(path, req, "browser")


class SafeExtractionTests(unittest.TestCase):
    def archive(self, path: Path, name: str, *, symlink: bool = False) -> None:
        with tarfile.open(path, "w") as archive:
            item = tarfile.TarInfo(name)
            if symlink:
                item.type = tarfile.SYMTYPE
                item.linkname = "/etc/passwd"
                archive.addfile(item)
            else:
                data = b"content"
                item.size = len(data)
                item.mode = 0o644
                archive.addfile(item, io.BytesIO(data))

    def test_regular_file_is_extracted_without_extractall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "bundle.tar"
            self.archive(archive, "workspace/job-scout/code.py")
            destination = root / "out"
            release.safe_extract(archive, destination)
            self.assertEqual(
                (destination / "workspace/job-scout/code.py").read_bytes(), b"content"
            )

    def test_traversal_and_symlinks_are_rejected(self) -> None:
        for name, symlink in (("../escape", False), ("workspace/link", True)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive = root / "bundle.tar"
                self.archive(archive, name, symlink=symlink)
                with self.assertRaises(release.ReleaseError):
                    release.safe_extract(archive, root / "out")

    def docker_export_archive(self, path: Path) -> None:
        """A tar shaped like `docker export` of the scratch bundle image.

        Besides the release payload, the container runtime injects /etc/mtab as
        a SYMLINK to /proc/mounts, /.dockerenv, and empty /dev, /proc, /sys.
        """
        with tarfile.open(path, "w") as archive:
            for name in ("dev", "etc", "proc", "sys", "workspace", "workspace/bin"):
                directory = tarfile.TarInfo(name)
                directory.type = tarfile.DIRTYPE
                directory.mode = 0o755
                archive.addfile(directory)
            mtab = tarfile.TarInfo("etc/mtab")
            mtab.type = tarfile.SYMTYPE
            mtab.linkname = "/proc/mounts"
            archive.addfile(mtab)
            for name, data, mode in (
                (".dockerenv", b"", 0o644),
                ("etc/hostname", b"container\n", 0o644),
                ("job-scout-manifest.json", b"{}\n", 0o644),
                ("workspace/bin/job-scout", b"#!/bin/sh\n", 0o755),
            ):
                item = tarfile.TarInfo(name)
                item.size = len(data)
                item.mode = mode
                archive.addfile(item, io.BytesIO(data))

    def test_docker_export_symlink_noise_is_ignored_when_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "bundle.tar"
            self.docker_export_archive(archive)
            destination = root / "out"
            release.safe_extract(
                archive, destination,
                frozenset({"job-scout-manifest.json", "workspace"}),
            )
            self.assertEqual((destination / "job-scout-manifest.json").read_bytes(), b"{}\n")
            self.assertEqual(
                (destination / "workspace/bin/job-scout").stat().st_mode & 0o777, 0o755
            )
            self.assertEqual(
                sorted(path.name for path in destination.iterdir()),
                ["job-scout-manifest.json", "workspace"],
            )

    def test_docker_export_symlink_noise_still_fails_unscoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "bundle.tar"
            self.docker_export_archive(archive)
            with self.assertRaises(release.ReleaseError):
                release.safe_extract(archive, root / "out")

    def test_symlink_inside_the_scope_is_still_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "bundle.tar"
            self.archive(archive, "workspace/link", symlink=True)
            with self.assertRaises(release.ReleaseError):
                release.safe_extract(
                    archive, root / "out",
                    frozenset({"job-scout-manifest.json", "workspace"}),
                )

    def test_traversal_is_rejected_even_when_out_of_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "bundle.tar"
            self.archive(archive, "../escape")
            with self.assertRaises(release.ReleaseError):
                release.safe_extract(
                    archive, root / "out", frozenset({"workspace"})
                )

    def test_invalid_extraction_scope_is_rejected(self) -> None:
        for scope in ("workspace/bin", "..", ""):
            with self.subTest(scope=scope), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive = root / "bundle.tar"
                self.archive(archive, "workspace/job-scout/code.py")
                with self.assertRaises(release.ReleaseError):
                    release.safe_extract(archive, root / "out", frozenset({scope}))

    def test_candidate_archive_round_trip_preserves_executable_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root / "candidate"
            wrapper = candidate / "bundle/workspace/bin/job-scout"
            wrapper.parent.mkdir(parents=True)
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.chmod(0o755)
            (candidate / "job-scout-deploy-request.json").write_text("{}\n", encoding="utf-8")
            archive = root / "candidate.tar"
            release.pack_candidate(candidate, archive)
            extracted = root / "extracted"
            release.safe_extract(archive, extracted)
            self.assertEqual(
                (extracted / "bundle/workspace/bin/job-scout").stat().st_mode & 0o777,
                0o755,
            )


class RollbackLedgerTests(unittest.TestCase):
    def ledger(self, req: dict[str, object]) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "recordedAt": "2026-08-01T12:00:00Z",
            "genaebotSha": req["genaebotSha"],
            "receiverRunId": req["receiverRunId"],
            "receiverRunAttempt": req["receiverRunAttempt"],
            "request": req,
        }

    def test_rollback_inherits_only_a_recorded_release(self) -> None:
        previous = request()
        rolled_back = release.prepare_rollback_request(
            self.ledger(previous),
            manifest_hash=str(previous["manifestHash"]),
            genaebot_sha="3" * 40,
            receiver_run_id="111",
            receiver_run_attempt="2",
        )
        self.assertEqual(rolled_back["deploymentMode"], "rollback")
        for key in DIGESTS:
            self.assertEqual(rolled_back[key], previous[key])
        self.assertEqual(rolled_back["correlationId"], "rollback-111-2")

    def test_ui_input_is_manifest_hash_only(self) -> None:
        self.assertEqual(
            release.rollback_manifest_hash({"inputs": {"rollback_manifest_hash": "d" * 64}}),
            "d" * 64,
        )
        with self.assertRaises(release.ReleaseError):
            release.rollback_manifest_hash({
                "inputs": {
                    "rollback_manifest_hash": "d" * 64,
                    "browserDigest": DIGESTS["browserDigest"],
                }
            })

    def test_only_unexpired_main_ledger_artifacts_are_selected(self) -> None:
        name = "job-scout-production-" + "d" * 64
        response = {
            "artifacts": [
                {"id": 5, "name": name, "expired": True, "workflow_run": {"id": 50, "head_branch": "main"}},
                {"id": 6, "name": name, "expired": False, "workflow_run": {"id": 60, "head_branch": "feature"}},
                {"id": 7, "name": name, "expired": False, "workflow_run": {"id": 70, "head_branch": "main"}},
                {"id": 8, "name": "candidate", "expired": False, "workflow_run": {"id": 80, "head_branch": "main"}},
            ]
        }
        self.assertEqual(release.select_ledger_artifact(response, "d" * 64), (7, 70))
        with self.assertRaises(release.ReleaseError):
            release.select_ledger_artifact({"artifacts": []}, "d" * 64)

    def test_host_ledger_canonical_request_round_trip_authorizes_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "job-scout-production-ledger.jsonl"
            request_path = root / "job-scout-deploy-request.json"
            deployed = request()
            release._canonical_json(request_path, deployed)
            self.assertEqual(request_path.read_bytes(), release._canonical_json_bytes(deployed))
            request_hash = hashlib.sha256(request_path.read_bytes()).hexdigest()
            release.append_host_success(
                ledger_path,
                deployed,
                request_hash,
                recorded_at="2026-08-01T12:00:00Z",
            )

            loaded = release.load_host_ledger(ledger_path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["requestSha256"], request_hash)
            rollback = release.prepare_rollback_request(
                self.ledger(deployed),
                manifest_hash=str(deployed["manifestHash"]),
                genaebot_sha="3" * 40,
                receiver_run_id="111",
                receiver_run_attempt="2",
            )
            release.authorize_host_request(ledger_path, rollback)

    def test_host_ledger_rejects_unrecorded_rollback_and_identity_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "job-scout-production-ledger.jsonl"
            rollback = request(deploymentMode="rollback", correlationId="rollback-111-1")
            with self.assertRaisesRegex(release.ReleaseError, "not present"):
                release.authorize_host_request(ledger_path, rollback)

            deployed = request()
            request_path = root / "request.json"
            release._canonical_json(request_path, deployed)
            release.append_host_success(
                ledger_path,
                deployed,
                hashlib.sha256(request_path.read_bytes()).hexdigest(),
                recorded_at="2026-08-01T12:00:00Z",
            )
            rollback["browserDigest"] = "sha256:" + "9" * 64
            with self.assertRaisesRegex(release.ReleaseError, "not present"):
                release.authorize_host_request(ledger_path, rollback)

    def test_host_ledger_hash_chain_tampering_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "ledger.jsonl"
            deployed = request()
            request_path = root / "request.json"
            release._canonical_json(request_path, deployed)
            release.append_host_success(
                ledger_path,
                deployed,
                hashlib.sha256(request_path.read_bytes()).hexdigest(),
                recorded_at="2026-08-01T12:00:00Z",
            )
            ledger_path.write_text(
                ledger_path.read_text(encoding="utf-8").replace(
                    "2026-08-01T12:00:00Z", "2026-08-01T13:00:00Z"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(release.ReleaseError, "entry hash"):
                release.load_host_ledger(ledger_path)

    def test_select_ledger_cli_emits_only_consumed_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            name = "job-scout-production-" + "d" * 64
            response = root / "response.json"
            response.write_text(json.dumps({
                "artifacts": [{
                    "id": 7,
                    "name": name,
                    "expired": False,
                    "workflow_run": {"id": 70, "head_branch": "main"},
                }]
            }), encoding="utf-8")
            output = root / "output"
            completed = subprocess.run(
                [
                    sys.executable, str(MODULE_PATH), "select-ledger",
                    "--api-response", str(response),
                    "--manifest-hash", "d" * 64,
                    "--github-output", str(output),
                ],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), "run_id=70\n")

    def test_prepare_rollback_cli_requires_and_validates_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _request_path, bundle, deployed = write_bound_request(root)
            ledger_path = root / "production-ledger.json"
            ledger_path.write_text(json.dumps(self.ledger(deployed)), encoding="utf-8")
            rollback_path = root / "rollback-request.json"
            output = root / "output"
            completed = subprocess.run(
                [
                    sys.executable, str(MODULE_PATH), "prepare-rollback",
                    "--ledger", str(ledger_path),
                    "--manifest", str(bundle / "job-scout-manifest.json"),
                    "--manifest-hash", str(deployed["manifestHash"]),
                    "--genaebot-sha", "3" * 40,
                    "--receiver-run-id", "111",
                    "--receiver-run-attempt", "2",
                    "--request-out", str(rollback_path),
                    "--github-output", str(output),
                ],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(rollback_path.read_text())["deploymentMode"], "rollback")
            self.assertIn("deployment_mode=rollback\n", output.read_text(encoding="utf-8"))


class WorkflowAndWrapperContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.wrapper = (ROOT / "deploy/github-actions/openclaw-prod-deploy").read_text(encoding="utf-8")

    # The dispatched Job Scout release receiver jobs (`job-scout-release`,
    # `job-scout-deploy`, `record-job-scout-release`) and their
    # `repository_dispatch` / `workflow_dispatch` triggers have been REMOVED
    # from .github/workflows/deploy-main.yml. They ran on the self-hosted
    # `openclaw-prod` host that was stopped at the 2026-08-20 Railway cutover,
    # and the upstream repository no longer dispatches `job_scout_release`, so
    # the jobs were unreachable. The release/rollback/safe-extract helpers in
    # deploy/job_scout_release.py and the openclaw-prod-deploy wrapper are
    # retained (pending removal) and their behaviour is still covered by the
    # unit tests above; only the workflow wiring assertions are dropped here.

    def test_dispatched_receiver_jobs_and_triggers_are_removed(self) -> None:
        for job in ("job-scout-release:", "job-scout-deploy:", "record-job-scout-release:"):
            self.assertNotIn(f"\n  {job}", self.workflow)
        self.assertNotIn("repository_dispatch", self.workflow)
        self.assertNotIn("workflow_dispatch", self.workflow)
        self.assertNotIn("github.event.client_payload", self.workflow)
        self.assertNotIn("rollback_manifest_hash", self.workflow)

    def test_workflow_only_runs_on_pull_request_and_main_push(self) -> None:
        on_section = self.workflow[: self.workflow.index("permissions:")]
        self.assertIn("pull_request:", on_section)
        self.assertIn("push:", on_section)
        self.assertIn("- main", on_section)

    def test_root_wrapper_is_the_only_privileged_deploy_entrypoint(self) -> None:
        # The workflow no longer invokes any privileged deploy step; the
        # constrained root wrapper remains the sole privileged entrypoint for
        # the retained manual/self-hosted deploy path.
        self.assertNotIn("sudo ", self.workflow)
        self.assertIn("--job-scout-request", self.wrapper)
        self.assertIn("JOB_SCOUT_BUNDLE_DIR=\"$bundle_dir\"", self.wrapper)
        self.assertIn("verify-request", self.wrapper)
        self.assertIn("authorize-host-request", self.wrapper)
        self.assertLess(
            self.wrapper.index("authorize-host-request"),
            self.wrapper.index('"$src/deploy/openclaw-deploy.sh" --validated-head'),
        )
        self.assertGreater(
            self.wrapper.index("record-host-success"),
            self.wrapper.index('"$src/deploy/openclaw-deploy.sh" --validated-head'),
        )
        self.assertIn('python3 "$src/deploy/job_scout_consumer.py" validate "$bundle_dir"', self.wrapper)

    def test_wrapper_bootstrap_is_documented_and_source_controlled(self) -> None:
        installer = (ROOT / "deploy/github-actions/install-openclaw-prod-deploy-wrapper.sh").read_text(encoding="utf-8")
        docs = (ROOT / "deploy/github-actions/README.md").read_text(encoding="utf-8")
        self.assertIn("install -o root -g root -m 0755", installer)
        self.assertIn("job-scout-production-ledger.jsonl", installer)
        self.assertIn("visudo -cf", installer)
        self.assertIn("install-openclaw-prod-deploy-wrapper.sh", docs)
        self.assertIn("sudo cmp", docs)


if __name__ == "__main__":
    unittest.main()
