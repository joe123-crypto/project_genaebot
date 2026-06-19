#!/usr/bin/env bash
set -euo pipefail

OC_DIR="${OPENCLAW_DIR:-/home/joseph/.openclaw}"
DEPLOY_DIR="${OPENCLAW_DEPLOY_DIR:-/home/joseph/openclaw-deploy}"
STAGE_DIR="${OPENCLAW_STAGE_DIR:-$DEPLOY_DIR/live-source}"
STRICT=0

usage() {
  cat <<'EOF'
Usage: check-openclaw-mirror.sh [--strict]

Checks the generated deploy mirror at ~/openclaw-deploy/live-source against the
canonical source repo at ~/.openclaw. Without --strict, findings are warnings.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --strict)
      STRICT=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'ERROR: unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
  shift
done

set +e
OPENCLAW_MIRROR_SOURCE_ROOT="$OC_DIR" \
OPENCLAW_MIRROR_STAGE_ROOT="$STAGE_DIR" \
python3 - <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

source_root = Path(os.environ["OPENCLAW_MIRROR_SOURCE_ROOT"])
stage_root = Path(os.environ["OPENCLAW_MIRROR_STAGE_ROOT"])
manifest_path = stage_root / ".openclaw-source-manifest.json"
marker_path = stage_root / ".OPENCLAW_GENERATED"
runtime_names = {"state.json", "pending-links.json", "subscriptions.json"}
issues: list[str] = []

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def issue(message: str) -> None:
    issues.append(message)

if not stage_root.exists():
    issue(f"deploy mirror is missing: {stage_root}")
else:
    if not marker_path.exists():
        issue(f"generated marker missing: {marker_path}")
    if not manifest_path.exists():
        issue(f"source manifest missing: {manifest_path}")

    for path in sorted(stage_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(stage_root).as_posix()
        if (
            "__pycache__" in path.parts
            or path.suffix == ".pyc"
            or path.suffix == ".log"
            or path.name in runtime_names
        ):
            issue(f"runtime/cache file present in mirror: {rel}")

    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            issue(f"could not read source manifest: {exc}")
            manifest = {}

        manifest_files = {
            str(item.get("path")): item
            for item in manifest.get("files", [])
            if isinstance(item, dict) and item.get("path")
        }

        for rel, item in sorted(manifest_files.items()):
            stage_file = stage_root / rel
            source_rel = item.get("source")
            expected_hash = item.get("sha256")
            if not stage_file.exists():
                issue(f"manifest file missing from mirror: {rel}")
                continue
            try:
                stage_hash = sha256(stage_file)
            except Exception as exc:
                issue(f"could not hash mirror file {rel}: {exc}")
                continue
            if expected_hash and stage_hash != expected_hash:
                issue(f"mirror file changed after generation: {rel}")
            if not source_rel:
                issue(f"manifest file has no source mapping: {rel}")
                continue
            source_file = source_root / str(source_rel)
            if not source_file.exists():
                issue(f"source file missing for mirror entry: {source_rel}")
                continue
            try:
                source_hash = sha256(source_file)
            except Exception as exc:
                issue(f"could not hash source file {source_rel}: {exc}")
                continue
            if expected_hash and source_hash != expected_hash:
                issue(f"mirror stale: {rel} differs from {source_rel}")

        allowed_meta = {".OPENCLAW_GENERATED", ".openclaw-source-manifest.json"}
        for path in sorted(stage_root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(stage_root).as_posix()
            if rel in allowed_meta:
                continue
            if rel not in manifest_files:
                issue(f"mirror file is not listed in manifest: {rel}")

        invite_wrapper = stage_root / "workspace/bin/whatsapp-invite"
        if invite_wrapper.exists():
            try:
                wrapper_text = invite_wrapper.read_text(encoding="utf-8")
            except Exception as exc:
                issue(f"could not read invite wrapper: {exc}")
            else:
                for target in re.findall(r"exec\s+python3\s+/home/joseph/\.openclaw/(workspace/[\w./-]+)", wrapper_text):
                    target_file = stage_root / target
                    if not target_file.exists():
                        issue(f"invite wrapper points at unstaged workspace file: {target}")

if issues:
    print("OpenClaw deploy mirror warnings:")
    for message in issues:
        print(f"- {message}")
    raise SystemExit(1)

print("OpenClaw deploy mirror OK")
PY
rc=$?
set -e

if [ "$rc" -ne 0 ] && [ "$STRICT" = "1" ]; then
  exit "$rc"
fi
