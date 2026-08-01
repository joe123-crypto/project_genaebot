#!/usr/bin/env python3
"""Validate Job Scout release dispatches, deploy requests, and rollback ledgers."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
from typing import Any


SCHEMA_VERSION = 1
GENAEBOT_REPOSITORY = "joe123-crypto/project_genaebot"
JOB_SCOUT_REPOSITORY = "joe123-crypto/genaie-job-scout"
JOB_SCOUT_REPOSITORY_URL = f"https://github.com/{JOB_SCOUT_REPOSITORY}"
GHCR_ROOT = f"ghcr.io/{JOB_SCOUT_REPOSITORY}"

PAYLOAD_KEYS = frozenset(
    {
        "jobScoutSha",
        "bundleDigest",
        "browserDigest",
        "listenerDigest",
        "manifestHash",
        "sourceRunId",
        "correlationId",
    }
)
REQUEST_KEYS = PAYLOAD_KEYS | {
    "schemaVersion",
    "deploymentMode",
    "genaebotSha",
    "receiverRunId",
    "receiverRunAttempt",
}
LEDGER_KEYS = frozenset(
    {
        "schemaVersion",
        "recordedAt",
        "genaebotSha",
        "receiverRunId",
        "receiverRunAttempt",
        "request",
    }
)
HOST_LEDGER_ENTRY_KEYS = frozenset(
    {
        "schemaVersion",
        "sequence",
        "recordedAt",
        "requestSha256",
        "previousEntryHash",
        "request",
        "entryHash",
    }
)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")
CORRELATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[^\r\n]{1,64}$")

MAX_ARCHIVE_FILES = 20_000
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024


class ReleaseError(ValueError):
    """Raised when release data violates the deployment contract."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseError(f"JSON root must be an object: {path}")
    return value


def _exact_keys(value: dict[str, Any], expected: frozenset[str], where: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ReleaseError(f"{where} keys mismatch; missing={missing}, extra={extra}")


def _string(value: Any, pattern: re.Pattern[str], name: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ReleaseError(f"invalid {name}")
    return value


def _validate_payload(payload: dict[str, Any]) -> dict[str, str]:
    _exact_keys(payload, PAYLOAD_KEYS, "repository_dispatch client_payload")
    normalized = {
        "jobScoutSha": _string(payload["jobScoutSha"], SHA_RE, "jobScoutSha"),
        "bundleDigest": _string(payload["bundleDigest"], DIGEST_RE, "bundleDigest"),
        "browserDigest": _string(payload["browserDigest"], DIGEST_RE, "browserDigest"),
        "listenerDigest": _string(payload["listenerDigest"], DIGEST_RE, "listenerDigest"),
        "manifestHash": _string(payload["manifestHash"], HASH_RE, "manifestHash"),
        "sourceRunId": _string(payload["sourceRunId"], RUN_ID_RE, "sourceRunId"),
        "correlationId": _string(
            payload["correlationId"], CORRELATION_RE, "correlationId"
        ),
    }
    if len(set(normalized[key] for key in ("bundleDigest", "browserDigest", "listenerDigest"))) != 3:
        raise ReleaseError("the three artifact digests must be distinct")
    return normalized


def validate_request(request: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(request, REQUEST_KEYS, "deploy request")
    if request["schemaVersion"] != SCHEMA_VERSION:
        raise ReleaseError("unsupported deploy request schemaVersion")
    if request["deploymentMode"] not in {"release", "rollback"}:
        raise ReleaseError("deploymentMode must be release or rollback")
    normalized: dict[str, Any] = dict(_validate_payload({key: request[key] for key in PAYLOAD_KEYS}))
    normalized.update(
        {
            "schemaVersion": SCHEMA_VERSION,
            "deploymentMode": request["deploymentMode"],
            "genaebotSha": _string(request["genaebotSha"], SHA_RE, "genaebotSha"),
            "receiverRunId": _string(request["receiverRunId"], RUN_ID_RE, "receiverRunId"),
            "receiverRunAttempt": _string(
                request["receiverRunAttempt"], RUN_ID_RE, "receiverRunAttempt"
            ),
        }
    )
    return normalized


def _canonical_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_json_bytes(value))


def _canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _append_outputs(path: Path, values: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ReleaseError(f"newline in workflow output {key}")
            output.write(f"{key}={value}\n")


def artifact_references(request: dict[str, Any]) -> dict[str, str]:
    request = validate_request(request)
    return {
        "bundle_ref": f"{GHCR_ROOT}/openclaw-bundle@{request['bundleDigest']}",
        "browser_ref": f"{GHCR_ROOT}/browser@{request['browserDigest']}",
        "listener_ref": f"{GHCR_ROOT}/channel-listener@{request['listenerDigest']}",
    }


def parse_canonical_manifest(manifest_path: Path) -> dict[str, Any]:
    """Parse only the schemaVersion 1 shape committed by Job Scout WS3."""
    manifest = _load_json(manifest_path)
    if manifest.get("schemaVersion") != SCHEMA_VERSION:
        raise ReleaseError("unsupported Job Scout manifest schemaVersion")
    if "files" in manifest:
        raise ReleaseError("legacy Job Scout manifest files field is not schemaVersion 1")
    paths = manifest.get("paths")
    if not isinstance(paths, list) or not paths:
        raise ReleaseError("Job Scout manifest paths must be a non-empty array")
    for entry in paths:
        if not isinstance(entry, dict):
            raise ReleaseError("Job Scout manifest path entry must be an object")
        mode = entry.get("mode")
        executable = entry.get("executable")
        if mode not in {"0644", "0755"} or not isinstance(executable, bool):
            raise ReleaseError("Job Scout manifest path mode/executable shape mismatch")
        if executable is not (mode == "0755"):
            raise ReleaseError("Job Scout manifest path mode disagrees with executable")
    images = manifest.get("images")
    if not isinstance(images, dict) or set(images) != {"browser", "channel-listener"}:
        raise ReleaseError("Job Scout manifest images must be browser and channel-listener objects")
    references: dict[str, str] = {}
    for name in ("browser", "channel-listener"):
        image = images[name]
        if not isinstance(image, dict) or not isinstance(image.get("reference"), str):
            raise ReleaseError(f"Job Scout manifest image {name} must have a reference")
        references[name] = image["reference"]
    return {
        "repository": manifest.get("repository"),
        "commit": manifest.get("commit"),
        "browserReference": references["browser"],
        "listenerReference": references["channel-listener"],
    }


def parse_dispatch_event(
    event: dict[str, Any], *, genaebot_sha: str, receiver_run_id: str, receiver_run_attempt: str
) -> dict[str, Any]:
    if event.get("action") != "job_scout_release":
        raise ReleaseError("repository_dispatch action must be job_scout_release")
    repository = event.get("repository")
    if not isinstance(repository, dict) or repository.get("full_name") != GENAEBOT_REPOSITORY:
        raise ReleaseError("repository_dispatch targeted an unexpected repository")
    payload = event.get("client_payload")
    if not isinstance(payload, dict):
        raise ReleaseError("repository_dispatch client_payload must be an object")
    request: dict[str, Any] = _validate_payload(payload)
    request.update(
        {
            "schemaVersion": SCHEMA_VERSION,
            "deploymentMode": "release",
            "genaebotSha": _string(genaebot_sha, SHA_RE, "genaebotSha"),
            "receiverRunId": _string(receiver_run_id, RUN_ID_RE, "receiverRunId"),
            "receiverRunAttempt": _string(
                receiver_run_attempt, RUN_ID_RE, "receiverRunAttempt"
            ),
        }
    )
    return validate_request(request)


def rollback_manifest_hash(event: dict[str, Any]) -> str:
    inputs = event.get("inputs")
    if not isinstance(inputs, dict):
        raise ReleaseError("workflow_dispatch inputs must be an object")
    _exact_keys(inputs, frozenset({"rollback_manifest_hash"}), "workflow_dispatch inputs")
    return _string(inputs["rollback_manifest_hash"], HASH_RE, "rollback_manifest_hash")


def validate_ledger(ledger: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(ledger, LEDGER_KEYS, "production ledger")
    if ledger["schemaVersion"] != SCHEMA_VERSION:
        raise ReleaseError("unsupported production ledger schemaVersion")
    _string(ledger["recordedAt"], TIMESTAMP_RE, "recordedAt")
    _string(ledger["genaebotSha"], SHA_RE, "ledger genaebotSha")
    _string(ledger["receiverRunId"], RUN_ID_RE, "ledger receiverRunId")
    _string(ledger["receiverRunAttempt"], RUN_ID_RE, "ledger receiverRunAttempt")
    if not isinstance(ledger["request"], dict):
        raise ReleaseError("production ledger request must be an object")
    request = validate_request(ledger["request"])
    for key in ("genaebotSha", "receiverRunId", "receiverRunAttempt"):
        if ledger[key] != request[key]:
            raise ReleaseError(f"production ledger {key} does not match its request")
    normalized = dict(ledger)
    normalized["request"] = request
    return normalized


def prepare_rollback_request(
    ledger: dict[str, Any], *, manifest_hash: str, genaebot_sha: str,
    receiver_run_id: str, receiver_run_attempt: str
) -> dict[str, Any]:
    ledger = validate_ledger(ledger)
    selected_hash = _string(manifest_hash, HASH_RE, "rollback_manifest_hash")
    previous = ledger["request"]
    if previous["manifestHash"] != selected_hash:
        raise ReleaseError("selected rollback hash does not match the production ledger")
    request = dict(previous)
    request.update(
        {
            "deploymentMode": "rollback",
            "genaebotSha": _string(genaebot_sha, SHA_RE, "genaebotSha"),
            "receiverRunId": _string(receiver_run_id, RUN_ID_RE, "receiverRunId"),
            "receiverRunAttempt": _string(
                receiver_run_attempt, RUN_ID_RE, "receiverRunAttempt"
            ),
            "correlationId": f"rollback-{receiver_run_id}-{receiver_run_attempt}",
        }
    )
    return validate_request(request)


def select_ledger_artifact(response: dict[str, Any], manifest_hash: str) -> tuple[int, int]:
    expected_name = f"job-scout-production-{_string(manifest_hash, HASH_RE, 'manifest hash')}"
    artifacts = response.get("artifacts")
    if not isinstance(artifacts, list):
        raise ReleaseError("artifact API response is missing artifacts")
    candidates: list[tuple[int, int]] = []
    for item in artifacts:
        if not isinstance(item, dict) or item.get("name") != expected_name or item.get("expired") is not False:
            continue
        workflow_run = item.get("workflow_run")
        artifact_id = item.get("id")
        if not isinstance(workflow_run, dict) or workflow_run.get("head_branch") != "main":
            continue
        if not isinstance(artifact_id, int) or isinstance(artifact_id, bool) or artifact_id <= 0:
            continue
        run_id = workflow_run.get("id")
        if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
            continue
        candidates.append((artifact_id, run_id))
    if not candidates:
        raise ReleaseError("no unexpired production ledger exists for that manifest hash")
    return max(candidates)


def _host_entry_hash(entry_without_hash: dict[str, Any]) -> str:
    encoded = json.dumps(
        entry_without_hash, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_host_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if not path.is_file() or path.is_symlink():
        raise ReleaseError("host production ledger must be a regular file")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ReleaseError(f"could not read host production ledger: {exc}") from exc
    entries: list[dict[str, Any]] = []
    previous_hash = "0" * 64
    for index, line in enumerate(lines, start=1):
        if not line:
            raise ReleaseError("host production ledger contains a blank line")
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReleaseError(f"invalid host ledger JSON at line {index}") from exc
        if not isinstance(entry, dict):
            raise ReleaseError(f"host ledger line {index} must be an object")
        _exact_keys(entry, HOST_LEDGER_ENTRY_KEYS, f"host ledger line {index}")
        if entry["schemaVersion"] != SCHEMA_VERSION or entry["sequence"] != index:
            raise ReleaseError(f"host ledger sequence/schema mismatch at line {index}")
        _string(entry["recordedAt"], TIMESTAMP_RE, "host ledger recordedAt")
        _string(entry["requestSha256"], HASH_RE, "host ledger requestSha256")
        if entry["previousEntryHash"] != previous_hash:
            raise ReleaseError(f"host ledger hash chain mismatch at line {index}")
        if not isinstance(entry["request"], dict):
            raise ReleaseError(f"host ledger request must be an object at line {index}")
        entry["request"] = validate_request(entry["request"])
        if hashlib.sha256(_canonical_json_bytes(entry["request"])).hexdigest() != entry["requestSha256"]:
            raise ReleaseError(f"host ledger request hash mismatch at line {index}")
        declared_hash = _string(entry["entryHash"], HASH_RE, "host ledger entryHash")
        body = {key: value for key, value in entry.items() if key != "entryHash"}
        if _host_entry_hash(body) != declared_hash:
            raise ReleaseError(f"host ledger entry hash mismatch at line {index}")
        previous_hash = declared_hash
        entries.append(entry)
    return entries


def authorize_host_request(ledger_path: Path, request: dict[str, Any]) -> None:
    request = validate_request(request)
    entries = load_host_ledger(ledger_path)
    if request["deploymentMode"] != "rollback":
        return
    identity_keys = (
        "jobScoutSha",
        "bundleDigest",
        "browserDigest",
        "listenerDigest",
        "manifestHash",
    )
    if not any(
        all(entry["request"][key] == request[key] for key in identity_keys)
        for entry in entries
    ):
        raise ReleaseError(
            "rollback release is not present in the host successful-release ledger"
        )


def append_host_success(
    ledger_path: Path, request: dict[str, Any], request_sha256: str,
    recorded_at: str | None = None
) -> dict[str, Any]:
    request = validate_request(request)
    request_hash = _string(request_sha256, HASH_RE, "request hash")
    if hashlib.sha256(_canonical_json_bytes(request)).hexdigest() != request_hash:
        raise ReleaseError("request hash is not the canonical request SHA-256")
    timestamp = recorded_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _string(timestamp, TIMESTAMP_RE, "host ledger recordedAt")
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(ledger_path, flags, 0o600)
    try:
        with os.fdopen(fd, "r+", encoding="utf-8", closefd=False) as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            entries = load_host_ledger(ledger_path)
            previous_hash = entries[-1]["entryHash"] if entries else "0" * 64
            body = {
                "schemaVersion": SCHEMA_VERSION,
                "sequence": len(entries) + 1,
                "recordedAt": timestamp,
                "requestSha256": request_hash,
                "previousEntryHash": previous_hash,
                "request": request,
            }
            entry = {**body, "entryHash": _host_entry_hash(body)}
            encoded = (json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
            os.write(handle.fileno(), encoded)
            os.fsync(handle.fileno())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return entry
    finally:
        os.close(fd)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_manifest_request(manifest_path: Path, request: dict[str, Any]) -> None:
    request = validate_request(request)
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ReleaseError("release is missing a regular job-scout-manifest.json")
    if _sha256(manifest_path) != request["manifestHash"]:
        raise ReleaseError("manifest SHA-256 does not match deploy request")
    manifest = parse_canonical_manifest(manifest_path)
    if manifest["repository"] != JOB_SCOUT_REPOSITORY_URL:
        raise ReleaseError("bundle manifest repository mismatch")
    if manifest["commit"] != request["jobScoutSha"]:
        raise ReleaseError("bundle manifest commit mismatch")
    expected = artifact_references(request)
    if manifest["browserReference"] != expected["browser_ref"]:
        raise ReleaseError("bundle manifest browser image mismatch")
    if manifest["listenerReference"] != expected["listener_ref"]:
        raise ReleaseError("bundle manifest listener image mismatch")


def verify_request_bundle(
    request_path: Path, bundle: Path, expected_request_hash: str,
    expected_genaebot_sha: str | None = None
) -> dict[str, Any]:
    expected_hash = _string(expected_request_hash, HASH_RE, "request hash")
    if _sha256(request_path) != expected_hash:
        raise ReleaseError("deploy request SHA-256 mismatch")
    request = validate_request(_load_json(request_path))
    if request_path.read_bytes() != _canonical_json_bytes(request):
        raise ReleaseError("deploy request JSON is not canonical")
    if expected_genaebot_sha is not None and request["genaebotSha"] != _string(
        expected_genaebot_sha, SHA_RE, "expected genaebot SHA"
    ):
        raise ReleaseError("deploy request is for a different genaebot commit")
    verify_manifest_request(bundle / "job-scout-manifest.json", request)
    return request


def verify_image_inspect(inspect_path: Path, request: dict[str, Any], artifact: str) -> None:
    expected_refs = artifact_references(request)
    ref_key = {
        "openclaw-bundle": "bundle_ref",
        "browser": "browser_ref",
        "channel-listener": "listener_ref",
    }.get(artifact)
    if ref_key is None:
        raise ReleaseError("unknown Job Scout artifact type")
    value = json.loads(inspect_path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise ReleaseError("docker image inspect must contain exactly one image")
    image = value[0]
    if expected_refs[ref_key] not in image.get("RepoDigests", []):
        raise ReleaseError(f"pulled {artifact} image does not expose the requested digest")
    config = image.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    expected_labels = {
        "org.opencontainers.image.source": JOB_SCOUT_REPOSITORY_URL,
        "org.opencontainers.image.revision": request["jobScoutSha"],
        "io.genaie.jobscout.artifact": artifact,
    }
    if not isinstance(labels, dict):
        raise ReleaseError(f"pulled {artifact} image has no labels")
    for key, expected in expected_labels.items():
        if labels.get(key) != expected:
            raise ReleaseError(f"pulled {artifact} image label {key} mismatch")


def safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    seen: set[str] = set()
    count = 0
    total = 0
    with tarfile.open(archive, "r:*") as source:
        for member in source:
            raw_name = member.name.removeprefix("./")
            if raw_name in {"", "."}:
                continue
            if raw_name == ".dockerenv" and member.isfile() and member.size == 0:
                continue
            path = PurePosixPath(raw_name)
            if (
                path.is_absolute()
                or "\\" in raw_name
                or "\x00" in raw_name
                or any(ord(character) < 32 or ord(character) == 127 for character in raw_name)
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise ReleaseError(f"unsafe bundle archive path: {member.name!r}")
            normalized = path.as_posix()
            if normalized in seen:
                raise ReleaseError(f"duplicate bundle archive path: {normalized}")
            seen.add(normalized)
            count += 1
            if count > MAX_ARCHIVE_FILES:
                raise ReleaseError("bundle archive contains too many entries")
            if member.isdir():
                (destination / normalized).mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ReleaseError(f"bundle archive contains a non-regular file: {normalized}")
            total += member.size
            if total > MAX_ARCHIVE_BYTES:
                raise ReleaseError("bundle archive exceeds the extraction size limit")
            handle = source.extractfile(member)
            if handle is None:
                raise ReleaseError(f"could not read bundle archive member: {normalized}")
            target = destination / normalized
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                remaining = member.size
                while remaining:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ReleaseError(f"truncated bundle archive member: {normalized}")
                    output.write(chunk)
                    remaining -= len(chunk)
            os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)


def pack_candidate(candidate: Path, archive: Path) -> None:
    if archive.exists():
        raise ReleaseError(f"candidate archive already exists: {archive}")
    if not candidate.is_dir() or candidate.is_symlink():
        raise ReleaseError("candidate must be a regular directory")
    top_level = sorted(path.name for path in candidate.iterdir())
    if top_level != ["bundle", "job-scout-deploy-request.json"]:
        raise ReleaseError("candidate directory has unexpected top-level entries")
    files: list[Path] = []
    for path in candidate.rglob("*"):
        if path.is_symlink():
            raise ReleaseError("candidate directory contains a symlink")
        if path.is_file():
            files.append(path)
        elif not path.is_dir():
            raise ReleaseError("candidate directory contains a special file")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w", format=tarfile.GNU_FORMAT) as output:
        for path in sorted(files):
            relative = path.relative_to(candidate).as_posix()
            data = path.read_bytes()
            item = tarfile.TarInfo(relative)
            item.size = len(data)
            item.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
            item.mtime = 0
            item.uid = item.gid = 0
            item.uname = item.gname = ""
            output.addfile(item, io.BytesIO(data))


def _write_request_outputs(output: Path, request: dict[str, Any]) -> None:
    refs = artifact_references(request)
    _append_outputs(
        output,
        {
            **refs,
            "manifest_hash": request["manifestHash"],
            "job_scout_sha": request["jobScoutSha"],
            "correlation_id": request["correlationId"],
            "deployment_mode": request["deploymentMode"],
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    dispatch = sub.add_parser("parse-dispatch")
    dispatch.add_argument("--event", type=Path, required=True)
    dispatch.add_argument("--genaebot-sha", required=True)
    dispatch.add_argument("--receiver-run-id", required=True)
    dispatch.add_argument("--receiver-run-attempt", required=True)
    dispatch.add_argument("--request-out", type=Path, required=True)
    dispatch.add_argument("--github-output", type=Path, required=True)

    selector = sub.add_parser("rollback-selector")
    selector.add_argument("--event", type=Path, required=True)
    selector.add_argument("--github-output", type=Path, required=True)

    select = sub.add_parser("select-ledger")
    select.add_argument("--api-response", type=Path, required=True)
    select.add_argument("--manifest-hash", required=True)
    select.add_argument("--github-output", type=Path, required=True)

    rollback = sub.add_parser("prepare-rollback")
    rollback.add_argument("--ledger", type=Path, required=True)
    rollback.add_argument("--manifest", type=Path, required=True)
    rollback.add_argument("--manifest-hash", required=True)
    rollback.add_argument("--genaebot-sha", required=True)
    rollback.add_argument("--receiver-run-id", required=True)
    rollback.add_argument("--receiver-run-attempt", required=True)
    rollback.add_argument("--request-out", type=Path, required=True)
    rollback.add_argument("--github-output", type=Path, required=True)

    verify = sub.add_parser("verify-request")
    verify.add_argument("--request", type=Path, required=True)
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--request-sha256", required=True)
    verify.add_argument("--genaebot-sha")

    image = sub.add_parser("verify-image")
    image.add_argument("--inspect", type=Path, required=True)
    image.add_argument("--request", type=Path, required=True)
    image.add_argument(
        "--artifact", choices=("openclaw-bundle", "browser", "channel-listener"), required=True
    )

    extract = sub.add_parser("safe-extract")
    extract.add_argument("--archive", type=Path, required=True)
    extract.add_argument("--destination", type=Path, required=True)

    pack = sub.add_parser("pack-candidate")
    pack.add_argument("--candidate", type=Path, required=True)
    pack.add_argument("--archive", type=Path, required=True)

    record = sub.add_parser("record-ledger")
    record.add_argument("--request", type=Path, required=True)
    record.add_argument("--recorded-at", required=True)
    record.add_argument("--ledger-out", type=Path, required=True)

    authorize = sub.add_parser("authorize-host-request")
    authorize.add_argument("--ledger", type=Path, required=True)
    authorize.add_argument("--request", type=Path, required=True)

    host_record = sub.add_parser("record-host-success")
    host_record.add_argument("--ledger", type=Path, required=True)
    host_record.add_argument("--request", type=Path, required=True)
    host_record.add_argument("--request-sha256", required=True)

    args = parser.parse_args(argv)
    if args.command == "parse-dispatch":
        request = parse_dispatch_event(
            _load_json(args.event),
            genaebot_sha=args.genaebot_sha,
            receiver_run_id=args.receiver_run_id,
            receiver_run_attempt=args.receiver_run_attempt,
        )
        _canonical_json(args.request_out, request)
        _write_request_outputs(args.github_output, request)
    elif args.command == "rollback-selector":
        manifest_hash = rollback_manifest_hash(_load_json(args.event))
        _append_outputs(
            args.github_output,
            {
                "manifest_hash": manifest_hash,
                "ledger_name": f"job-scout-production-{manifest_hash}",
            },
        )
    elif args.command == "select-ledger":
        _artifact_id, run_id = select_ledger_artifact(
            _load_json(args.api_response), args.manifest_hash
        )
        _append_outputs(args.github_output, {"run_id": str(run_id)})
    elif args.command == "prepare-rollback":
        ledger = validate_ledger(_load_json(args.ledger))
        verify_manifest_request(args.manifest, ledger["request"])
        request = prepare_rollback_request(
            ledger,
            manifest_hash=args.manifest_hash,
            genaebot_sha=args.genaebot_sha,
            receiver_run_id=args.receiver_run_id,
            receiver_run_attempt=args.receiver_run_attempt,
        )
        _canonical_json(args.request_out, request)
        _write_request_outputs(args.github_output, request)
    elif args.command == "verify-request":
        verify_request_bundle(
            args.request, args.bundle, args.request_sha256, args.genaebot_sha
        )
    elif args.command == "verify-image":
        verify_image_inspect(args.inspect, validate_request(_load_json(args.request)), args.artifact)
    elif args.command == "safe-extract":
        safe_extract(args.archive, args.destination)
    elif args.command == "pack-candidate":
        pack_candidate(args.candidate, args.archive)
    elif args.command == "record-ledger":
        request = validate_request(_load_json(args.request))
        recorded_at = _string(args.recorded_at, TIMESTAMP_RE, "recordedAt")
        ledger = {
            "schemaVersion": SCHEMA_VERSION,
            "recordedAt": recorded_at,
            "genaebotSha": request["genaebotSha"],
            "receiverRunId": request["receiverRunId"],
            "receiverRunAttempt": request["receiverRunAttempt"],
            "request": request,
        }
        _canonical_json(args.ledger_out, validate_ledger(ledger))
    elif args.command == "authorize-host-request":
        authorize_host_request(args.ledger, validate_request(_load_json(args.request)))
    elif args.command == "record-host-success":
        append_host_success(
            args.ledger,
            validate_request(_load_json(args.request)),
            args.request_sha256,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ReleaseError, OSError, tarfile.TarError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
