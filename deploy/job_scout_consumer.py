#!/usr/bin/env python3
"""Validate an externally supplied Job Scout release overlay (the WS4 consumer).

`genaebot` no longer owns Job Scout source. A Job Scout *release* ships an
immutable, non-running OCI "source bundle". The production receiver pulls that
bundle, `docker export`s it, and runs `deploy/job_scout_release.py safe-extract`
to unpack it into a directory. This module is the consumer half of that
contract: it strictly validates the extracted overlay against its own manifest
before the release is allowed onto a deploy stage.

  job_scout_consumer.py validate BUNDLE_DIR

BUNDLE_DIR is the safe-extracted bundle. Its relevant contents are:
  - job-scout-manifest.json   the versioned release contract (schemaVersion 1,
                              the `paths[]` shape), and
  - workspace/...             the Job Scout-owned overlay files it declares.

Validation (exit 0 = accepted; non-zero + a `REJECTED:` reason on stderr):
  * the manifest is canonical schemaVersion 1 JSON with the exact top-level,
    path-entry, image, and metadata field sets;
  * every declared path is safe (no traversal/absolute/backslash/NUL), rooted at
    `workspace/`, allowlisted, and free of runtime/private/secret content;
  * every declared file exists on disk with the exact sha256, byte count, and
    mode, and the `workspace/` subtree contains EXACTLY the declared file set
    (no extras, no symlinks, no non-regular files);
  * the required entrypoints/locks are present and the four live-path wrappers
    are executable.

The extracted root may ALSO contain Docker init-layer siblings (`dev/`, `etc/`,
`proc/`, `sys/`) that `docker export` injects and `safe-extract` preserves.
Those are never part of the release and are never composed onto a stage — the
deploy applies `workspace/` only — so validation is scoped to `workspace/` and
the manifest and deliberately tolerates such siblings. This is the one
intentional difference from Job Scout's `release/validate_manifest.py`, which
runs against a clean `docker cp` overlay in CI and can require a strict root.

Import-safe: no side effects at import, so deploy tests and shell wrappers can
reuse the exact same validation logic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
import re

MANIFEST_NAME = "job-scout-manifest.json"

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

SCHEMA_VERSION = 1
KNOWN_PROTOCOLS = {1}
OVERLAY_ROOT = "workspace"
REPOSITORY_URL = "https://github.com/joe123-crypto/genaie-job-scout"
REGISTRY_ROOT = "ghcr.io/joe123-crypto/genaie-job-scout"
TOP_LEVEL_KEYS = {
    "schemaVersion", "repository", "commit", "protocolVersion",
    "requiredLivePaths", "overlayRoot", "paths", "images", "metadata",
}
PATH_KEYS = {"path", "sha256", "bytes", "mode", "executable"}
IMAGE_KEYS = {"name", "digest", "reference"}
METADATA_KEYS = {"packagerVersion", "fileCount", "totalBytes"}
REQUIRED_LIVE_PATHS = {
    "/home/joseph/.openclaw/workspace/bin/job-scout",
    "/home/joseph/.openclaw/workspace/bin/job-scout-agent",
    "/home/joseph/.openclaw/workspace/bin/job-scout-channel-health",
    "/home/joseph/.openclaw/workspace/bin/job-scout-convert-pending",
}
LIVE_PATH_PREFIX = "/home/joseph/.openclaw/"
# Minimum entrypoints/locks/sources that MUST be present in a valid overlay.
REQUIRED_OVERLAY_FILES = {
    "workspace/job-scout/job_scout.py",
    "workspace/job-scout/package.json",
    "workspace/job-scout/package-lock.json",
    "workspace/job-scout/channel-listener/package.json",
    "workspace/job-scout/channel-listener/package-lock.json",
    "workspace/job-scout/web_apply_server.mjs",
    "workspace/job-scout/channel-listener/listener.mjs",
    "workspace/skills/job-scout/SKILL.md",
    "workspace/docs/job-scout-onboarding.md",
}
DENY_DIR_NAMES = {"__pycache__", "node_modules", ".venv", "venv", ".git", ".cache"}
DENY_SUFFIXES = (".pyc", ".log", ".session", ".creds", ".key", ".pem", ".env")
DENY_BASENAMES = {"state.json", "pending-links.json", "subscriptions.json", "creds.json"}


class Reject(Exception):
    """Raised when a release overlay violates the schemaVersion 1 contract."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    value: dict = {}
    for key, item in pairs:
        if key in value:
            raise Reject(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def load_canonical_manifest(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise Reject("overlay is missing a regular job-scout-manifest.json")
    try:
        raw = path.read_bytes()
        manifest = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except Reject:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise Reject(f"invalid manifest JSON: {error}") from error
    if not isinstance(manifest, dict):
        raise Reject("manifest root must be an object")
    canonical = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), indent=2) + "\n"
    ).encode("utf-8")
    if raw != canonical:
        raise Reject("manifest is not canonical schemaVersion 1 JSON")
    return manifest


def _safe_path(p: str) -> bool:
    if not p or p.startswith("/") or "\\" in p or "\x00" in p:
        return False
    if os.path.normpath(p) != p:
        return False
    if p.startswith("..") or "/../" in ("/" + p + "/"):
        return False
    return p == OVERLAY_ROOT or p.startswith(OVERLAY_ROOT + "/")


def _looks_runtime(p: str) -> bool:
    parts = p.split("/")
    if any(part in DENY_DIR_NAMES for part in parts):
        return True
    base = parts[-1]
    if base in DENY_BASENAMES or base.startswith(".env"):
        return True
    return any(base.endswith(s) for s in DENY_SUFFIXES)


def check_manifest(manifest: dict) -> None:
    """Strictly validate the manifest document against schemaVersion 1."""
    if not isinstance(manifest, dict) or set(manifest) != TOP_LEVEL_KEYS:
        raise Reject("manifest top-level fields do not match schemaVersion 1")
    if type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != SCHEMA_VERSION:
        raise Reject(f"unknown schemaVersion: {manifest.get('schemaVersion')}")
    if type(manifest.get("protocolVersion")) is not int or manifest["protocolVersion"] not in KNOWN_PROTOCOLS:
        raise Reject(f"unknown protocolVersion: {manifest.get('protocolVersion')}")

    repo = manifest.get("repository")
    if repo != REPOSITORY_URL:
        raise Reject(f"bad repository url: {repo!r}")
    commit = manifest.get("commit")
    if not isinstance(commit, str) or not _SHA40.match(commit):
        raise Reject(f"bad commit sha: {commit!r}")
    if manifest.get("overlayRoot") != OVERLAY_ROOT:
        raise Reject(f"bad overlayRoot: {manifest.get('overlayRoot')!r}")

    rlp = manifest.get("requiredLivePaths")
    if not isinstance(rlp, list) or set(rlp) != REQUIRED_LIVE_PATHS:
        raise Reject("requiredLivePaths mismatch")
    if rlp != sorted(rlp):
        raise Reject("requiredLivePaths not sorted")

    paths = manifest.get("paths")
    if not isinstance(paths, list) or not paths:
        raise Reject("paths must be a non-empty array")
    seen: set[str] = set()
    prev = ""
    total = 0
    present: set[str] = set()
    for entry in paths:
        if not isinstance(entry, dict) or set(entry) != PATH_KEYS:
            raise Reject("manifest path entry fields do not match schemaVersion 1")
        p = entry.get("path")
        if not isinstance(p, str) or not _safe_path(p):
            raise Reject(f"unsafe or bad path: {p!r}")
        if p < prev:
            raise Reject(f"paths not sorted at {p!r}")
        prev = p
        if p in seen:
            raise Reject(f"duplicate path: {p!r}")
        seen.add(p)
        if _looks_runtime(p):
            raise Reject(f"runtime/private file present: {p!r}")
        sha = entry.get("sha256")
        if not isinstance(sha, str) or not _SHA256.match(sha):
            raise Reject(f"bad sha256 for {p!r}")
        b = entry.get("bytes")
        if type(b) is not int or b < 0:
            raise Reject(f"bad bytes for {p!r}")
        mode = entry.get("mode")
        if mode not in ("0644", "0755"):
            raise Reject(f"bad mode for {p!r}: {mode!r}")
        if entry.get("executable") is not (mode == "0755"):
            raise Reject(f"executable/mode disagree for {p!r}")
        total += b
        present.add(p)

    missing = REQUIRED_OVERLAY_FILES - present
    if missing:
        raise Reject(f"required overlay files missing: {sorted(missing)}")

    have = {e["path"]: e for e in paths}
    for live in REQUIRED_LIVE_PATHS:
        rel = live[len(LIVE_PATH_PREFIX):]
        e = have.get(rel)
        if e is None:
            raise Reject(f"required live path not in overlay: {live}")
        if e["mode"] != "0755":
            raise Reject(f"required live path not executable: {live}")

    images = manifest.get("images")
    if not isinstance(images, dict) or set(images) != {"browser", "channel-listener"}:
        raise Reject("images must have exactly browser + channel-listener")
    for name, img in images.items():
        if not isinstance(img, dict) or set(img) != IMAGE_KEYS or img.get("name") != name:
            raise Reject(f"image {name} fields do not match schemaVersion 1")
        d = img.get("digest")
        if not isinstance(d, str) or not _DIGEST.match(d):
            raise Reject(f"image {name} digest not a sha256 digest: {d!r}")
        ref = img.get("reference", "")
        if ref != f"{REGISTRY_ROOT}/{name}@{d}":
            raise Reject(f"image {name} reference not digest-pinned: {ref!r}")

    meta = manifest.get("metadata")
    if not isinstance(meta, dict) or set(meta) != METADATA_KEYS:
        raise Reject("metadata must contain only packagerVersion, fileCount, totalBytes")
    if type(meta["fileCount"]) is not int or meta["fileCount"] != len(paths):
        raise Reject("metadata.fileCount != len(paths)")
    if type(meta["totalBytes"]) is not int or meta["totalBytes"] != total:
        raise Reject("metadata.totalBytes != sum(bytes)")


def verify_overlay(manifest: dict, bundle: Path) -> None:
    """Verify the extracted workspace/ subtree exactly matches the manifest.

    Non-``workspace/`` root siblings (Docker init-layer noise from
    ``docker export``: ``dev/``, ``etc/``, ``proc/``, ``sys/``) are ignored:
    they are never part of the release and never composed onto a stage.
    """
    ws = bundle / OVERLAY_ROOT
    if ws.is_symlink() or not ws.is_dir():
        raise Reject("overlay is missing a real workspace/ directory")

    declared = {e["path"]: e for e in manifest["paths"]}
    found: set[str] = set()
    found_dirs: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(ws):
        dirnames.sort()
        for dirname in dirnames:
            directory = Path(dirpath) / dirname
            rel_dir = str(directory.relative_to(bundle))
            if directory.is_symlink() or not directory.is_dir():
                raise Reject(f"invalid directory in overlay: {rel_dir}")
            found_dirs.add(rel_dir)
        for fn in filenames:
            abs_p = Path(dirpath) / fn
            rel = str(abs_p.relative_to(bundle))
            found.add(rel)
            metadata = abs_p.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise Reject(f"non-regular file in overlay: {rel}")
            if rel not in declared:
                raise Reject(f"extra file not in manifest: {rel}")
            data = abs_p.read_bytes()
            e = declared[rel]
            if hashlib.sha256(data).hexdigest() != e["sha256"]:
                raise Reject(f"hash mismatch: {rel}")
            if len(data) != e["bytes"]:
                raise Reject(f"byte-count mismatch: {rel}")
            actual = f"{stat.S_IMODE(metadata.st_mode):04o}"
            if actual != e["mode"]:
                raise Reject(f"mode mismatch: {rel} ({actual} != {e['mode']})")
    missing = set(declared) - found
    if missing:
        raise Reject(f"declared files missing from overlay: {sorted(missing)}")
    expected_dirs: set[str] = set()
    for rel in declared:
        parent = Path(rel).parent
        while parent.as_posix() != OVERLAY_ROOT:
            expected_dirs.add(parent.as_posix())
            parent = parent.parent
    if found_dirs != expected_dirs:
        raise Reject(
            f"overlay directory set differs: extra={sorted(found_dirs - expected_dirs)}, "
            f"missing={sorted(expected_dirs - found_dirs)}"
        )


def validate_bundle(bundle: Path) -> None:
    """Load, schema-check, and overlay-verify an extracted release bundle."""
    manifest = load_canonical_manifest(bundle / MANIFEST_NAME)
    check_manifest(manifest)
    verify_overlay(manifest, bundle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Job Scout release overlay consumer")
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate", help="validate an extracted release bundle overlay")
    v.add_argument("bundle", type=Path, help="the safe-extracted bundle directory")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            bundle = args.bundle
            if bundle.is_symlink() or not bundle.is_dir():
                raise Reject(f"bundle is not a directory: {bundle}")
            validate_bundle(bundle)
    except Reject as error:
        print(f"REJECTED: {error}", file=sys.stderr)
        return 1
    print("Job Scout release overlay accepted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
