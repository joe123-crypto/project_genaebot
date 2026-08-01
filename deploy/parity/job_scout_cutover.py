#!/usr/bin/env python3
"""Local-only Job Scout parity evidence and rollback validation tooling."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
from typing import Any


SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
WORKFLOW_RUN_RE = re.compile(r"[1-9][0-9]*")
FORBIDDEN_COMPONENTS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "auth",
    "cache",
    "caches",
    "credentials",
    "dispatch-artifacts",
    "dispatch-runs",
    "job-scout-data",
    "logs",
    "node_modules",
    "runtime",
    "secrets",
    "state",
    "users",
}
FORBIDDEN_NAMES = {
    "creds.json",
    "pending-links.json",
    "state.json",
    "subscriptions.json",
}
FORBIDDEN_SUFFIXES = {".creds", ".env", ".key", ".log", ".pem", ".pyc", ".session"}
EXPECTED_CRONS = {
    "job-scout": {
        "expr": "0 8 * * *",
        "tz": "Africa/Harare",
        "timeout": 21600,
        "argv": [
            "/home/joseph/.openclaw/workspace/bin/job-scout-agent",
            "--daily-live",
        ],
    },
    "job-scout-approvals": {
        "expr": "0 */5 * * * *",
        "tz": "Africa/Algiers",
        "timeout": 21600,
        "argv": [
            "/home/joseph/.openclaw/workspace/bin/job-scout-agent",
            "--approved-live",
        ],
    },
    "job-scout-group-feed": {
        "expr": "20 9 * * *",
        "tz": "Africa/Harare",
        "timeout": 600,
        "argvPrefix": [
            "/home/joseph/.openclaw/workspace/bin/job-scout",
            "deliver-group-feed",
            "--target",
        ],
        "argvSuffix": ["--refresh-if-stale"],
    },
    "job-scout-cv-conversion": {
        "expr": "0 * * * * *",
        "tz": "Africa/Algiers",
        "timeout": 600,
        "argv": [
            "/home/joseph/.openclaw/workspace/bin/job-scout-convert-pending",
        ],
    },
    "job-scout-channel-health": {
        "expr": "*/15 * * * *",
        "tz": "Africa/Harare",
        "timeout": 30,
        "argv": [
            "/home/joseph/.openclaw/workspace/bin/job-scout-channel-health",
            "--max-age-seconds",
            "180",
        ],
    },
}


class ValidationError(ValueError):
    """Raised when local cutover evidence is incomplete or unsafe."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationError(f"invalid {label} JSON at {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be a JSON object: {path}")
    return value


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(value, indent=2, sort_keys=True).encode("utf-8"))
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative_path(raw: object, label: str = "path") -> str:
    if not isinstance(raw, str) or not raw:
        raise ValidationError(f"{label} must be a non-empty string")
    if "\\" in raw or "\x00" in raw:
        raise ValidationError(f"unsafe {label}: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValidationError(f"unsafe {label}: {raw!r}")
    normalized = path.as_posix()
    if normalized != raw or raw.startswith("/") or "//" in raw:
        raise ValidationError(f"non-canonical {label}: {raw!r}")
    return normalized


def reject_runtime_path(rel: str) -> None:
    path = PurePosixPath(rel)
    lowered_parts = {part.casefold() for part in path.parts}
    if lowered_parts & FORBIDDEN_COMPONENTS:
        raise ValidationError(f"runtime/private/cache path is forbidden in an overlay: {rel}")
    name = path.name.casefold()
    if name in FORBIDDEN_NAMES or name.startswith(".env") or path.suffix.casefold() in FORBIDDEN_SUFFIXES:
        raise ValidationError(f"runtime/private/cache file is forbidden in an overlay: {rel}")


def require_plain_directory(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        raise ValidationError(f"{label} is unavailable: {path}: {error}") from error
    if stat.S_ISLNK(mode):
        raise ValidationError(f"{label} must not be a symlink: {path}")
    if not stat.S_ISDIR(mode):
        raise ValidationError(f"{label} must be a directory: {path}")


def require_regular_beneath(root: Path, rel: str, label: str) -> Path:
    current = root
    parts = PurePosixPath(rel).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError as error:
            raise ValidationError(f"missing {label}: {rel}: {error}") from error
        if stat.S_ISLNK(mode):
            raise ValidationError(f"symlink is forbidden for {label}: {rel}")
        if index < len(parts) - 1 and not stat.S_ISDIR(mode):
            raise ValidationError(f"non-directory path component for {label}: {rel}")
    if not stat.S_ISREG(mode):
        raise ValidationError(f"{label} must be a regular file: {rel}")
    return current


def file_record(root: Path, rel: str, label: str) -> dict[str, object]:
    path = require_regular_beneath(root, rel, label)
    before = path.stat()
    digest = sha256_file(path)
    after = path.stat()
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity:
        raise ValidationError(f"{label} changed while it was being hashed: {rel}")
    actual_mode = stat.S_IMODE(after.st_mode)
    if actual_mode not in {0o644, 0o755}:
        raise ValidationError(
            f"{label} has unsupported on-disk mode {actual_mode:04o}; expected 0644 or 0755: {rel}"
        )
    return {
        "path": rel,
        "sha256": digest,
        "bytes": after.st_size,
        "mode": f"{actual_mode:04o}",
        "executable": actual_mode == 0o755,
    }


def scan_regular_tree(root: Path, label: str) -> tuple[dict[str, dict[str, object]], set[str]]:
    require_plain_directory(root, label)
    files: dict[str, dict[str, object]] = {}
    directories: set[str] = set()
    for current_raw, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(current_raw)
        rel_current = current.relative_to(root)
        for name in sorted(dirnames):
            child = current / name
            rel = (rel_current / name).as_posix()
            mode = child.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ValidationError(f"symlink is forbidden in {label}: {rel}")
            if not stat.S_ISDIR(mode):
                raise ValidationError(f"non-directory entry in {label}: {rel}")
            directories.add(rel)
        for name in sorted(filenames):
            child = current / name
            rel = (rel_current / name).as_posix()
            mode = child.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ValidationError(f"symlink is forbidden in {label}: {rel}")
            if not stat.S_ISREG(mode):
                raise ValidationError(f"non-regular file is forbidden in {label}: {rel}")
            reject_runtime_path(rel)
            files[rel] = file_record(root, rel, label)
    return files, directories


def load_allowlist(path: Path) -> dict[str, Any]:
    payload = read_json(path, "ownership allowlist")
    if type(payload.get("schemaVersion")) is not int or payload["schemaVersion"] != 1:
        raise ValidationError("ownership allowlist schemaVersion must be 1")
    repository = payload.get("repository")
    if not isinstance(repository, str) or not repository:
        raise ValidationError("ownership allowlist repository must be non-empty")
    protocol = payload.get("protocolVersion")
    if not isinstance(protocol, int) or isinstance(protocol, bool) or protocol < 1:
        raise ValidationError("ownership allowlist protocolVersion must be a positive integer")
    if payload.get("manifestContractCommit") != "9459a9b":
        raise ValidationError("ownership allowlist must pin the frozen WS3 manifest contract at 9459a9b")
    overlay_root = safe_relative_path(payload.get("overlayRoot"), "ownership allowlist overlayRoot")
    if overlay_root != "workspace":
        raise ValidationError("ownership allowlist overlayRoot must be workspace")

    normalized: dict[str, list[str]] = {}
    for field in ("trees", "files", "requiredFiles", "sourceRemovalPaths"):
        raw_values = payload.get(field)
        if not isinstance(raw_values, list):
            raise ValidationError(f"ownership allowlist {field} must be a list")
        values = [safe_relative_path(value, f"allowlist {field} entry") for value in raw_values]
        if len(values) != len(set(values)):
            raise ValidationError(f"ownership allowlist {field} contains duplicates")
        for rel in values:
            reject_runtime_path(rel)
        normalized[field] = values

    trees = normalized["trees"]
    for index, tree in enumerate(trees):
        for other in trees[index + 1:]:
            if other.startswith(f"{tree}/") or tree.startswith(f"{other}/"):
                raise ValidationError(f"ownership allowlist trees overlap: {tree}, {other}")
    for rel in normalized["files"]:
        if any(rel == tree or rel.startswith(f"{tree}/") for tree in trees):
            raise ValidationError(f"ownership allowlist file is already covered by a tree: {rel}")

    required_live_paths = payload.get("requiredLivePaths")
    if not isinstance(required_live_paths, list) or not required_live_paths:
        raise ValidationError("ownership allowlist requiredLivePaths must be a non-empty list")
    if required_live_paths != sorted(required_live_paths) or len(required_live_paths) != len(set(required_live_paths)):
        raise ValidationError("ownership allowlist requiredLivePaths must be unique and sorted")
    for live_path in required_live_paths:
        if not isinstance(live_path, str) or not live_path.startswith("/home/joseph/.openclaw/workspace/"):
            raise ValidationError(f"invalid required live path in ownership allowlist: {live_path!r}")

    payload.update(normalized)
    return payload


def is_owned(rel: str, allowlist: dict[str, Any]) -> bool:
    if rel in allowlist["files"]:
        return True
    return any(rel.startswith(f"{tree}/") for tree in allowlist["trees"])


def collect_curated_files(root: Path, allowlist: dict[str, Any]) -> dict[str, dict[str, object]]:
    require_plain_directory(root, "curated stage root")
    records: dict[str, dict[str, object]] = {}
    for rel in allowlist["files"]:
        records[rel] = file_record(root, rel, "curated stage file")
    for tree in allowlist["trees"]:
        tree_path = root / tree
        require_plain_directory(tree_path, f"curated ownership tree {tree}")
        tree_files, _ = scan_regular_tree(tree_path, f"curated ownership tree {tree}")
        for child_rel, record in tree_files.items():
            rel = f"{tree}/{child_rel}"
            reject_runtime_path(rel)
            record = dict(record)
            record["path"] = rel
            records[rel] = record
    missing_required = sorted(set(allowlist["requiredFiles"]) - records.keys())
    if missing_required:
        raise ValidationError(f"curated stage is missing required Job Scout files: {missing_required}")
    return records


def load_release_manifest(path: Path, allowlist: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, object]]]:
    payload = read_json(path, "Job Scout release manifest")
    if type(payload.get("schemaVersion")) is not int or payload["schemaVersion"] != 1:
        raise ValidationError("Job Scout release manifest schemaVersion must be 1")
    if payload.get("repository") != allowlist["repository"]:
        raise ValidationError(
            f"Job Scout release manifest repository must be {allowlist['repository']!r}"
        )
    commit = payload.get("commit")
    if not isinstance(commit, str) or SHA_RE.fullmatch(commit) is None:
        raise ValidationError("Job Scout release manifest commit must be an exact lowercase 40-character SHA")
    if (
        type(payload.get("protocolVersion")) is not int
        or payload["protocolVersion"] != allowlist["protocolVersion"]
    ):
        raise ValidationError("Job Scout release manifest integration protocol is incompatible")
    if payload.get("overlayRoot") != allowlist["overlayRoot"]:
        raise ValidationError("Job Scout release manifest overlayRoot is incompatible")
    required_live_paths = payload.get("requiredLivePaths")
    if required_live_paths != allowlist["requiredLivePaths"]:
        raise ValidationError("Job Scout release manifest requiredLivePaths mismatch")
    if required_live_paths != sorted(required_live_paths):
        raise ValidationError("Job Scout release manifest requiredLivePaths must be sorted")
    raw_files = payload.get("paths")
    if not isinstance(raw_files, list) or not raw_files:
        raise ValidationError("Job Scout release manifest paths must be a non-empty list")

    records: dict[str, dict[str, object]] = {}
    previous_path = ""
    for index, raw in enumerate(raw_files):
        if not isinstance(raw, dict):
            raise ValidationError(f"manifest paths[{index}] must be an object")
        rel = safe_relative_path(raw.get("path"), f"manifest paths[{index}].path")
        reject_runtime_path(rel)
        if not rel.startswith(f"{allowlist['overlayRoot']}/"):
            raise ValidationError(f"manifest path is outside overlayRoot: {rel}")
        if rel < previous_path:
            raise ValidationError(f"manifest paths are not sorted at: {rel}")
        previous_path = rel
        if not is_owned(rel, allowlist):
            raise ValidationError(f"manifest path is outside the Job Scout ownership allowlist: {rel}")
        if rel in records:
            raise ValidationError(f"manifest contains duplicate path: {rel}")
        digest = raw.get("sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValidationError(f"manifest has invalid SHA-256 for {rel}")
        byte_count = raw.get("bytes")
        if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 0:
            raise ValidationError(f"manifest has invalid byte count for {rel}")
        mode = raw.get("mode")
        if mode not in {"0644", "0755"}:
            raise ValidationError(f"manifest has invalid mode for {rel}: {mode!r}")
        executable = raw.get("executable")
        if not isinstance(executable, bool) or executable is not (mode == "0755"):
            raise ValidationError(f"manifest mode and executable disagree for {rel}")
        records[rel] = {
            "path": rel,
            "sha256": digest,
            "bytes": byte_count,
            "mode": mode,
            "executable": executable,
        }

    missing_required = sorted(set(allowlist["requiredFiles"]) - records.keys())
    if missing_required:
        raise ValidationError(f"Job Scout release manifest is missing required files: {missing_required}")
    for live_path in allowlist["requiredLivePaths"]:
        rel = live_path.removeprefix("/home/joseph/.openclaw/")
        record = records.get(rel)
        if record is None or record["mode"] != "0755" or record["executable"] is not True:
            raise ValidationError(f"required live wrapper is missing or non-executable: {live_path}")

    images = payload.get("images")
    if not isinstance(images, dict) or set(images) != {"browser", "channel-listener"}:
        raise ValidationError("Job Scout release manifest images must contain exactly browser and channel-listener")
    for name in ("browser", "channel-listener"):
        image = images[name]
        if not isinstance(image, dict):
            raise ValidationError(f"Job Scout release manifest image must be structured: {name}")
        if image.get("name") != name:
            raise ValidationError(f"Job Scout release manifest image name mismatch: {name}")
        digest = image.get("digest")
        if not isinstance(digest, str) or DIGEST_RE.fullmatch(digest) is None:
            raise ValidationError(f"Job Scout release manifest image digest is invalid: {name}")
        reference = image.get("reference")
        expected_reference = f"ghcr.io/joe123-crypto/genaie-job-scout/{name}@{digest}"
        if reference != expected_reference:
            raise ValidationError(f"Job Scout release manifest image reference is not digest-pinned: {name}")

    metadata = payload.get("metadata")
    if not isinstance(metadata, dict) or set(metadata) != {"packagerVersion", "fileCount", "totalBytes"}:
        raise ValidationError("Job Scout release manifest metadata shape is invalid")
    if metadata.get("packagerVersion") != "1":
        raise ValidationError("Job Scout release manifest packagerVersion must be 1")
    if type(metadata.get("fileCount")) is not int or metadata["fileCount"] != len(raw_files):
        raise ValidationError("Job Scout release manifest metadata.fileCount mismatch")
    if (
        type(metadata.get("totalBytes")) is not int
        or metadata["totalBytes"] != sum(int(record["bytes"]) for record in records.values())
    ):
        raise ValidationError("Job Scout release manifest metadata.totalBytes mismatch")
    return payload, records


def expected_directories(paths: set[str]) -> set[str]:
    directories: set[str] = set()
    for rel in paths:
        parent = PurePosixPath(rel).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def inventory_digest(records: dict[str, dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for rel in sorted(records):
        record = records[rel]
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(record["sha256"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(record["bytes"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(record["mode"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(b"1" if record["executable"] else b"0")
        digest.update(b"\n")
    return digest.hexdigest()


def validate_bundle(
    bundle_root: Path,
    manifest_path: Path,
    allowlist: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, object]]]:
    require_plain_directory(bundle_root, "bundle root")
    if manifest_path.parent.absolute() != bundle_root.absolute() or manifest_path.name != "job-scout-manifest.json":
        raise ValidationError("release manifest must be bundle-root/job-scout-manifest.json")
    require_regular_beneath(bundle_root, manifest_path.name, "release manifest")
    manifest, declared = load_release_manifest(manifest_path, allowlist)
    actual, actual_dirs = scan_regular_tree(bundle_root, "bundle")
    actual.pop("job-scout-manifest.json", None)
    declared_paths = set(declared)
    actual_paths = set(actual)
    extras = sorted(actual_paths - declared_paths)
    missing = sorted(declared_paths - actual_paths)
    if extras or missing:
        raise ValidationError(f"bundle file set differs from manifest: extras={extras}, missing={missing}")
    allowed_dirs = expected_directories(declared_paths)
    extra_dirs = sorted(actual_dirs - allowed_dirs)
    if extra_dirs:
        raise ValidationError(f"bundle contains undeclared directories: {extra_dirs}")
    for rel in sorted(declared):
        if actual[rel] != declared[rel]:
            raise ValidationError(
                f"bundle metadata differs from manifest for {rel}: declared={declared[rel]}, actual={actual[rel]}"
            )
    return manifest, actual


def compare_overlay(
    bundle_root: Path,
    curated_root: Path,
    manifest_path: Path,
    allowlist_path: Path,
) -> dict[str, Any]:
    allowlist = load_allowlist(allowlist_path)
    manifest, bundle_records = validate_bundle(bundle_root, manifest_path, allowlist)
    curated_records = collect_curated_files(curated_root, allowlist)
    bundle_paths = set(bundle_records)
    curated_paths = set(curated_records)
    extras = sorted(bundle_paths - curated_paths)
    missing = sorted(curated_paths - bundle_paths)
    if extras or missing:
        raise ValidationError(f"bundle differs from curated ownership set: extras={extras}, missing={missing}")
    for rel in sorted(bundle_records):
        if bundle_records[rel] != curated_records[rel]:
            raise ValidationError(
                f"bundle differs from curated stage for {rel}: bundle={bundle_records[rel]}, curated={curated_records[rel]}"
            )
    byte_count = sum(int(record["bytes"]) for record in bundle_records.values())
    return {
        "schemaVersion": 1,
        "kind": "job-scout-overlay-parity",
        "result": "pass",
        "recordedAt": utc_now(),
        "repository": manifest["repository"],
        "commit": manifest["commit"],
        "manifestSha256": sha256_file(manifest_path),
        "fileCount": len(bundle_records),
        "bytes": byte_count,
        "inventorySha256": inventory_digest(bundle_records),
        "checks": [
            "exact-path-set",
            "sha256",
            "bytes",
            "mode",
            "executable-mode",
            "no-extras",
            "no-symlinks",
            "safe-owned-paths",
        ],
    }


def runtime_snapshot(root: Path) -> dict[str, Any]:
    require_plain_directory(root, "runtime root")
    aggregate = hashlib.sha256()
    counts = {"entries": 0, "files": 0, "directories": 0, "symlinks": 0, "bytes": 0}
    for current_raw, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(current_raw)
        rel_current = current.relative_to(root)
        dirnames.sort()
        for name in list(dirnames):
            path = current / name
            rel = (rel_current / name).as_posix()
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                kind = "symlink"
                value = os.readlink(path)
                dirnames.remove(name)
                counts["symlinks"] += 1
            elif stat.S_ISDIR(metadata.st_mode):
                kind = "directory"
                value = ""
                counts["directories"] += 1
            else:
                raise ValidationError(f"unsupported runtime entry type: {rel}")
            counts["entries"] += 1
            aggregate.update(f"{kind}\0{rel}\0{stat.S_IMODE(metadata.st_mode):04o}\0{value}\n".encode("utf-8"))
        for name in sorted(filenames):
            path = current / name
            rel = (rel_current / name).as_posix()
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                kind = "symlink"
                value = os.readlink(path)
                counts["symlinks"] += 1
                size = 0
            elif stat.S_ISREG(metadata.st_mode):
                kind = "file"
                before = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
                value = sha256_file(path)
                after_metadata = path.stat()
                after = (
                    after_metadata.st_dev,
                    after_metadata.st_ino,
                    after_metadata.st_size,
                    after_metadata.st_mtime_ns,
                )
                if before != after:
                    raise ValidationError(f"runtime file changed while it was being hashed: {rel}")
                size = after_metadata.st_size
                metadata = after_metadata
                counts["files"] += 1
                counts["bytes"] += size
            else:
                raise ValidationError(f"unsupported runtime entry type: {rel}")
            counts["entries"] += 1
            aggregate.update(
                f"{kind}\0{rel}\0{stat.S_IMODE(metadata.st_mode):04o}\0{size}\0{value}\n".encode("utf-8")
            )
    return {
        "schemaVersion": 1,
        "kind": "job-scout-runtime-snapshot",
        "capturedAt": utc_now(),
        "root": str(root.absolute()),
        "digest": aggregate.hexdigest(),
        **counts,
    }


def load_runtime_snapshot(path: Path) -> dict[str, Any]:
    snapshot = read_json(path, "runtime snapshot")
    if snapshot.get("schemaVersion") != 1 or snapshot.get("kind") != "job-scout-runtime-snapshot":
        raise ValidationError(f"unsupported runtime snapshot: {path}")
    digest = snapshot.get("digest")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValidationError(f"invalid runtime snapshot digest: {path}")
    for field in ("entries", "files", "directories", "symlinks", "bytes"):
        value = snapshot.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValidationError(f"invalid runtime snapshot {field}: {path}")
    if not isinstance(snapshot.get("root"), str) or not snapshot["root"]:
        raise ValidationError(f"invalid runtime snapshot root: {path}")
    validate_timestamp(snapshot.get("capturedAt"), f"runtime snapshot capturedAt: {path}")
    return snapshot


def assert_runtime_unchanged(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    compared_fields = ("root", "digest", "entries", "files", "directories", "symlinks", "bytes")
    differences = [field for field in compared_fields if before.get(field) != after.get(field)]
    if differences:
        raise ValidationError(f"runtime changed across cutover evidence capture: {differences}")
    return {
        "root": before["root"],
        "beforeCapturedAt": before.get("capturedAt"),
        "afterCapturedAt": after.get("capturedAt"),
        "digest": before["digest"],
        "entries": before["entries"],
        "files": before["files"],
        "directories": before["directories"],
        "symlinks": before["symlinks"],
        "bytes": before["bytes"],
        "unchanged": True,
    }


def assert_runtime_observation_window(
    before: dict[str, Any],
    after: dict[str, Any],
    deployment_time: str,
) -> None:
    before_time = parse_timestamp(before["capturedAt"], "runtime before capturedAt")
    after_time = parse_timestamp(after["capturedAt"], "runtime after capturedAt")
    deployed = parse_timestamp(deployment_time, "deployment time")
    if before_time >= after_time:
        raise ValidationError("runtime before/after capture timestamps must be strictly ordered")
    if not (before_time <= deployed <= after_time):
        raise ValidationError("runtime captures must bracket the declared deployment time")


def normalize_crons(snapshot_path: Path) -> list[dict[str, Any]]:
    payload = read_json(snapshot_path, "OpenClaw cron snapshot")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise ValidationError("OpenClaw cron snapshot must contain a jobs list")
    selected: dict[str, dict[str, Any]] = {}
    for job in jobs:
        if not isinstance(job, dict):
            raise ValidationError("OpenClaw cron snapshot contains a non-object job")
        name = job.get("name")
        if name not in EXPECTED_CRONS:
            continue
        if name in selected:
            raise ValidationError(f"OpenClaw cron snapshot contains duplicate schedule: {name}")
        selected[name] = job
    missing = sorted(set(EXPECTED_CRONS) - selected.keys())
    if missing:
        raise ValidationError(f"OpenClaw cron snapshot is missing Job Scout schedules: {missing}")

    normalized: list[dict[str, Any]] = []
    for name, expected in EXPECTED_CRONS.items():
        job = selected[name]
        schedule = job.get("schedule")
        payload_value = job.get("payload")
        if not isinstance(schedule, dict) or not isinstance(payload_value, dict):
            raise ValidationError(f"cron schedule or payload is malformed: {name}")
        if job.get("enabled") is not True:
            raise ValidationError(f"cron schedule must be enabled: {name}")
        if schedule.get("expr") != expected["expr"] or schedule.get("tz") != expected["tz"]:
            raise ValidationError(f"cron cadence or timezone differs from baseline: {name}")
        if job.get("sessionTarget") != "isolated":
            raise ValidationError(f"cron session target differs from baseline: {name}")
        if payload_value.get("kind") != "command":
            raise ValidationError(f"cron payload kind differs from baseline: {name}")
        if payload_value.get("cwd") != "/home/joseph/.openclaw/workspace":
            raise ValidationError(f"cron command working directory differs from baseline: {name}")
        if payload_value.get("timeoutSeconds") != expected["timeout"]:
            raise ValidationError(f"cron timeout differs from baseline: {name}")
        argv = payload_value.get("argv")
        if "argv" in expected:
            if argv != expected["argv"]:
                raise ValidationError(f"cron command differs from baseline: {name}")
        else:
            prefix = expected["argvPrefix"]
            suffix = expected["argvSuffix"]
            if (
                not isinstance(argv, list)
                or len(argv) != len(prefix) + len(suffix) + 1
                or argv[: len(prefix)] != prefix
                or argv[-len(suffix):] != suffix
                or not isinstance(argv[len(prefix)], str)
                or not argv[len(prefix)]
            ):
                raise ValidationError(f"cron command differs from baseline: {name}")
        failure_alert = job.get("failureAlert")
        if (
            not isinstance(failure_alert, dict)
            or failure_alert.get("after") != 1
            or failure_alert.get("channel") != "whatsapp"
            or not isinstance(failure_alert.get("to"), str)
            or not failure_alert["to"].strip()
            or failure_alert.get("cooldown") != "1h"
        ):
            raise ValidationError(f"cron failure alert differs from baseline: {name}")
        normalized.append(deepcopy(job))
    return normalized


def validate_sha(value: str, label: str) -> str:
    if SHA_RE.fullmatch(value) is None:
        raise ValidationError(f"{label} must be an exact lowercase 40-character SHA")
    return value


def validate_digest(value: str, label: str) -> str:
    if DIGEST_RE.fullmatch(value) is None:
        raise ValidationError(f"{label} must be an immutable sha256 digest")
    return value


def validate_workflow_run_id(value: str, label: str) -> str:
    if WORKFLOW_RUN_RE.fullmatch(value) is None:
        raise ValidationError(f"{label} must be a positive GitHub workflow run ID")
    return value


def parse_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be an ISO-8601 timestamp with a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValidationError(f"{label} must be an ISO-8601 timestamp with a timezone") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{label} must include a timezone")
    return parsed


def validate_timestamp(value: object, label: str) -> str:
    parse_timestamp(value, label)
    assert isinstance(value, str)
    return value


def require_records_directory(path: Path) -> None:
    if path.exists():
        require_plain_directory(path, "rollback records directory")
    else:
        path.mkdir(parents=True, mode=0o700)


def load_catalog(records_dir: Path) -> dict[str, Any]:
    catalog_path = records_dir / "catalog.json"
    if not catalog_path.exists():
        return {"schemaVersion": 1, "records": []}
    if catalog_path.is_symlink() or not catalog_path.is_file():
        raise ValidationError("rollback catalog must be a regular file")
    catalog = read_json(catalog_path, "rollback catalog")
    if catalog.get("schemaVersion") != 1 or not isinstance(catalog.get("records"), list):
        raise ValidationError("rollback catalog schema is invalid")
    expected_sequence = 1
    seen_ids: set[str] = set()
    for entry in catalog["records"]:
        if not isinstance(entry, dict) or entry.get("sequence") != expected_sequence:
            raise ValidationError("rollback catalog sequence is invalid")
        record_id = entry.get("recordId")
        if not isinstance(record_id, str) or not record_id or record_id in seen_ids:
            raise ValidationError("rollback catalog record IDs are invalid")
        seen_ids.add(record_id)
        expected_sequence += 1
    return catalog


def copy_validated_bundle(
    bundle_root: Path,
    destination: Path,
    records: dict[str, dict[str, object]],
) -> None:
    destination.mkdir(parents=True, mode=0o700)
    for rel, record in sorted(records.items()):
        source = require_regular_beneath(bundle_root, rel, "bundle file")
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        os.chmod(target, stat.S_IMODE(source.stat().st_mode))
        if file_record(destination, rel, "archived overlay file") != record:
            raise ValidationError(f"archived overlay copy differs from bundle: {rel}")
    manifest_source = require_regular_beneath(bundle_root, "job-scout-manifest.json", "release manifest")
    shutil.copyfile(manifest_source, destination / "job-scout-manifest.json")
    os.chmod(destination / "job-scout-manifest.json", 0o644)


def record_evidence(args: argparse.Namespace) -> dict[str, Any]:
    if args.deployment_status not in {"local-rehearsal", "production-success"}:
        raise ValidationError("deployment status must be local-rehearsal or production-success")
    allowlist = load_allowlist(args.allowlist)
    manifest, bundle_records = validate_bundle(args.bundle_root, args.manifest, allowlist)
    parity = compare_overlay(args.bundle_root, args.curated_root, args.manifest, args.allowlist)
    same_snapshot = Path(os.path.abspath(args.runtime_before)) == Path(os.path.abspath(args.runtime_after))
    if not same_snapshot:
        try:
            same_snapshot = os.path.samefile(args.runtime_before, args.runtime_after)
        except OSError:
            same_snapshot = False
    if same_snapshot:
        raise ValidationError("runtime before and after snapshots must be distinct files")
    before = load_runtime_snapshot(args.runtime_before)
    after = load_runtime_snapshot(args.runtime_after)
    deployment_time = validate_timestamp(args.deployment_time, "deployment time")
    assert_runtime_observation_window(before, after, deployment_time)
    runtime = assert_runtime_unchanged(before, after)
    crons = normalize_crons(args.cron_snapshot)

    genaebot_sha = validate_sha(args.genaebot_sha, "genaebot SHA")
    job_scout_sha = validate_sha(args.job_scout_sha, "Job Scout SHA")
    if manifest["commit"] != job_scout_sha:
        raise ValidationError("Job Scout composite SHA differs from the release manifest commit")
    if manifest["images"]["browser"]["digest"] != args.browser_digest:
        raise ValidationError("browser composite digest differs from the structured release manifest image")
    if manifest["images"]["channel-listener"]["digest"] != args.listener_digest:
        raise ValidationError("channel listener composite digest differs from the structured release manifest image")
    composite = {
        "genaebotCommit": genaebot_sha,
        "jobScoutCommit": job_scout_sha,
        "sourceBundleDigest": validate_digest(args.source_bundle_digest, "source bundle digest"),
        "browserDigest": validate_digest(args.browser_digest, "browser digest"),
        "channelListenerDigest": validate_digest(args.listener_digest, "channel listener digest"),
        "deploymentTime": deployment_time,
        "workflowRunIds": {
            "jobScout": validate_workflow_run_id(args.job_scout_workflow_run_id, "Job Scout workflow run ID"),
            "genaebot": validate_workflow_run_id(args.genaebot_workflow_run_id, "genaebot workflow run ID"),
        },
        "correlationId": args.correlation_id,
    }
    if not isinstance(args.correlation_id, str) or not args.correlation_id.strip() or len(args.correlation_id) > 128:
        raise ValidationError("correlation ID must be between 1 and 128 characters")

    records_dir = args.records_dir
    require_records_directory(records_dir)
    catalog = load_catalog(records_dir)
    sequence = len(catalog["records"]) + 1
    record_id = f"record-{sequence:06d}-{job_scout_sha[:12]}"
    destination = records_dir / record_id
    if destination.exists():
        raise ValidationError(f"rollback record already exists: {record_id}")
    temporary = records_dir / f".{record_id}.tmp-{os.getpid()}"
    if temporary.exists():
        raise ValidationError(f"temporary rollback record already exists: {temporary}")

    recorded_at = utc_now()
    evidence = {
        "schemaVersion": 1,
        "kind": "job-scout-cutover-evidence",
        "recordId": record_id,
        "recordedAt": recorded_at,
        "deploymentStatus": args.deployment_status,
        "compositeRevision": composite,
        "parity": parity,
        "cronSchedules": crons,
        "runtime": runtime,
        "rollback": {
            "releaseManifest": f"{record_id}/release-manifest.json",
            "overlay": f"{record_id}/overlay",
            "runtimePolicy": "preserve",
        },
    }
    try:
        temporary.mkdir(mode=0o700)
        overlay_destination = temporary / "overlay"
        copy_validated_bundle(args.bundle_root, overlay_destination, bundle_records)
        shutil.copyfile(args.manifest, temporary / "release-manifest.json")
        os.chmod(temporary / "release-manifest.json", 0o644)
        write_json_atomic(temporary / "evidence.json", evidence)
        temporary.rename(destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    entry = {
        "sequence": sequence,
        "recordId": record_id,
        "deploymentStatus": args.deployment_status,
        "recordedAt": recorded_at,
        "deploymentTime": args.deployment_time,
        "genaebotCommit": genaebot_sha,
        "jobScoutCommit": job_scout_sha,
        "releaseManifestPath": f"{record_id}/release-manifest.json",
        "releaseManifestSha256": sha256_file(destination / "release-manifest.json"),
        "overlayPath": f"{record_id}/overlay",
        "overlayInventorySha256": inventory_digest(bundle_records),
        "evidencePath": f"{record_id}/evidence.json",
        "evidenceSha256": sha256_file(destination / "evidence.json"),
    }
    catalog["records"].append(entry)
    write_json_atomic(records_dir / "catalog.json", catalog)
    return evidence


def catalog_path(records_dir: Path, raw: object, label: str) -> Path:
    rel = safe_relative_path(raw, label)
    current = records_dir
    for part in PurePosixPath(rel).parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError as error:
            raise ValidationError(f"missing cataloged {label}: {rel}: {error}") from error
        if stat.S_ISLNK(mode):
            raise ValidationError(f"cataloged {label} must not traverse a symlink: {rel}")
    return current


def validate_rollback(args: argparse.Namespace) -> dict[str, Any]:
    records_dir = args.records_dir
    require_plain_directory(records_dir, "rollback records directory")
    catalog = load_catalog(records_dir)
    entries = {entry["recordId"]: entry for entry in catalog["records"]}
    current = entries.get(args.current_record_id)
    if current is None:
        raise ValidationError(f"current record is not in the rollback catalog: {args.current_record_id}")
    if current.get("deploymentStatus") != "production-success":
        raise ValidationError("current rollback record is not a successful production deployment")

    supplied_manifest = args.manifest
    if not supplied_manifest.is_absolute():
        supplied_manifest = records_dir / supplied_manifest
    supplied_absolute = Path(os.path.abspath(supplied_manifest))
    candidate = None
    for entry in catalog["records"]:
        expected = catalog_path(records_dir, entry.get("releaseManifestPath"), "release manifest")
        if Path(os.path.abspath(expected)) == supplied_absolute:
            candidate = entry
            break
    if candidate is None:
        raise ValidationError("rollback manifest is not a previously recorded catalog manifest")
    if candidate["deploymentStatus"] != "production-success":
        raise ValidationError("rollback manifest is not from a successful production deployment")
    if candidate["sequence"] >= current["sequence"]:
        raise ValidationError("rollback manifest must precede the current deployment record")
    if sha256_file(supplied_absolute) != candidate["releaseManifestSha256"]:
        raise ValidationError("rollback manifest hash differs from its catalog record")

    allowlist = load_allowlist(args.allowlist)
    overlay = catalog_path(records_dir, candidate.get("overlayPath"), "overlay")
    require_plain_directory(overlay, "cataloged rollback overlay")
    archived_manifest = overlay / "job-scout-manifest.json"
    manifest, records = validate_bundle(overlay, archived_manifest, allowlist)
    if inventory_digest(records) != candidate["overlayInventorySha256"]:
        raise ValidationError("rollback overlay inventory differs from its catalog record")
    if sha256_file(archived_manifest) != candidate["releaseManifestSha256"]:
        raise ValidationError("rollback overlay manifest differs from its recorded release manifest")

    evidence_path = catalog_path(records_dir, candidate.get("evidencePath"), "evidence")
    if sha256_file(evidence_path) != candidate["evidenceSha256"]:
        raise ValidationError("rollback evidence differs from its catalog record")
    evidence = read_json(evidence_path, "cutover evidence")
    selection = {
        "schemaVersion": 1,
        "kind": "job-scout-rollback-selection",
        "validatedAt": utc_now(),
        "currentRecordId": args.current_record_id,
        "selectedRecordId": candidate["recordId"],
        "releaseManifest": str(supplied_absolute),
        "releaseManifestSha256": candidate["releaseManifestSha256"],
        "overlay": str(overlay.absolute()),
        "overlayInventorySha256": candidate["overlayInventorySha256"],
        "compositeRevision": evidence["compositeRevision"],
        "cronSchedules": evidence["cronSchedules"],
        "runtime": evidence["runtime"],
        "runtimePolicy": "preserve",
        "jobScoutCommit": manifest["commit"],
    }
    return selection


def default_allowlist() -> Path:
    return Path(__file__).with_name("job-scout-owned-paths.json")


def add_common_bundle_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--curated-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--allowlist", type=Path, default=default_allowlist())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate local Job Scout cutover parity and immutable rollback evidence without deploying.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    compare_parser = subparsers.add_parser("compare", help="compare an extracted bundle to a curated stage")
    add_common_bundle_arguments(compare_parser)
    compare_parser.add_argument("--output", type=Path)

    runtime_parser = subparsers.add_parser("snapshot-runtime", help="record a read-only aggregate runtime fingerprint")
    runtime_parser.add_argument("--root", type=Path, required=True)
    runtime_parser.add_argument("--output", type=Path, required=True)

    record_parser = subparsers.add_parser("record", help="archive local parity and successful/deferred cutover evidence")
    add_common_bundle_arguments(record_parser)
    record_parser.add_argument("--runtime-before", type=Path, required=True)
    record_parser.add_argument("--runtime-after", type=Path, required=True)
    record_parser.add_argument("--cron-snapshot", type=Path, required=True)
    record_parser.add_argument("--records-dir", type=Path, required=True)
    record_parser.add_argument("--genaebot-sha", required=True)
    record_parser.add_argument("--job-scout-sha", required=True)
    record_parser.add_argument("--source-bundle-digest", required=True)
    record_parser.add_argument("--browser-digest", required=True)
    record_parser.add_argument("--listener-digest", required=True)
    record_parser.add_argument("--deployment-time", required=True)
    record_parser.add_argument("--job-scout-workflow-run-id", required=True)
    record_parser.add_argument("--genaebot-workflow-run-id", required=True)
    record_parser.add_argument("--correlation-id", required=True)
    record_parser.add_argument(
        "--deployment-status",
        choices=("local-rehearsal", "production-success"),
        required=True,
    )

    rollback_parser = subparsers.add_parser(
        "validate-rollback",
        help="select an earlier cataloged successful manifest without changing production",
    )
    rollback_parser.add_argument("--records-dir", type=Path, required=True)
    rollback_parser.add_argument("--manifest", type=Path, required=True)
    rollback_parser.add_argument("--current-record-id", required=True)
    rollback_parser.add_argument("--allowlist", type=Path, default=default_allowlist())
    rollback_parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "compare":
        result = compare_overlay(args.bundle_root, args.curated_root, args.manifest, args.allowlist)
    elif args.command == "snapshot-runtime":
        result = runtime_snapshot(args.root)
        write_json_atomic(args.output, result)
        print(f"Runtime snapshot written to {args.output}")
        return 0
    elif args.command == "record":
        result = record_evidence(args)
    elif args.command == "validate-rollback":
        result = validate_rollback(args)
    else:
        raise AssertionError(f"unsupported command: {args.command}")

    if getattr(args, "output", None):
        write_json_atomic(args.output, result)
        print(f"{result['kind']} written to {args.output}")
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValidationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
