#!/usr/bin/env python3
"""Reject reintroduced Job Scout-owned source after cutover activation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from job_scout_cutover import ValidationError, load_allowlist


ACTIVATION_NAME = "job-scout-source-removal.json"


def check_source_guard(
    repo_root: Path,
    allowlist_path: Path,
    activation_path: Path,
) -> tuple[bool, list[str]]:
    if not activation_path.exists():
        return False, []
    if activation_path.is_symlink() or not activation_path.is_file():
        raise ValidationError("Job Scout source-removal activation must be a regular file")
    try:
        activation = json.loads(activation_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationError(f"invalid Job Scout source-removal activation: {error}") from error
    if not isinstance(activation, dict) or activation.get("schemaVersion") != 1:
        raise ValidationError("Job Scout source-removal activation schemaVersion must be 1")
    if activation.get("enabled") is not True:
        raise ValidationError("Job Scout source-removal activation must set enabled to true")
    cutover_record_id = activation.get("cutoverRecordId")
    if not isinstance(cutover_record_id, str) or not cutover_record_id.strip():
        raise ValidationError("Job Scout source-removal activation requires a cutoverRecordId")

    allowlist = load_allowlist(allowlist_path)
    violations = []
    for rel in allowlist["sourceRemovalPaths"]:
        if (repo_root / rel).exists() or (repo_root / rel).is_symlink():
            violations.append(rel)
    return True, sorted(violations)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=Path(__file__).with_name("job-scout-owned-paths.json"),
    )
    parser.add_argument(
        "--activation",
        type=Path,
        default=Path(__file__).with_name(ACTIVATION_NAME),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    enabled, violations = check_source_guard(args.repo_root, args.allowlist, args.activation)
    if not enabled:
        print("Job Scout source-reintroduction guard skipped: cutover activation is absent")
        return 0
    if violations:
        print("ERROR: Job Scout-owned source was reintroduced after cutover:", file=sys.stderr)
        for rel in violations:
            print(f"- {rel}", file=sys.stderr)
        return 1
    print("Job Scout source-reintroduction guard OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValidationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
