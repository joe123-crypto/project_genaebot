#!/usr/bin/env python3
"""Portable repository, staging, and documentation-contract checks."""

from __future__ import annotations

import argparse
import fnmatch
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_WORKSPACE_PREFIX = "/home/joseph/.openclaw/workspace/"
FORBIDDEN_TRACKED_PARTS = {
    ".env",
    "credentials",
    "devices",
    "identity",
    "logs",
    "media",
    "memory",
    "node_modules",
    "plugin-state",
    "runtime",
    "sessions",
    "state",
}


class Checks:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def require(self, condition: bool, message: str) -> None:
        if not condition:
            self.errors.append(message)

    def finish(self) -> None:
        if self.errors:
            print("CI policy failures:", file=sys.stderr)
            for error in self.errors:
                print(f"- {error}", file=sys.stderr)
            raise SystemExit(1)
        print("Repository structure and documentation contracts OK")


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout


def tracked_files() -> dict[str, str]:
    files: dict[str, str] = {}
    for line in git("ls-files", "-s").splitlines():
        metadata, path = line.split("\t", 1)
        files[path] = metadata.split()[0]
    for path in git("ls-files", "--others", "--exclude-standard").splitlines():
        full = ROOT / path
        files[path] = "100755" if full.stat().st_mode & 0o111 else "100644"
    return files


def load_json(path: Path, checks: Checks) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        checks.errors.append(f"invalid JSON in {path.relative_to(ROOT)}: {error}")
        return None


def check_tracked_policy(files: dict[str, str], checks: Checks) -> None:
    for rel, mode in files.items():
        path = Path(rel)
        parts = set(path.parts)
        forbidden = sorted(parts & FORBIDDEN_TRACKED_PARTS)
        if forbidden or path.name.startswith(".env") or path.name == "openclaw.json":
            checks.errors.append(f"private/runtime path is tracked: {rel}")

        full = ROOT / rel
        if rel.startswith("workspace/bin/") and path.name != "test_genaie_mail.py":
            checks.require(mode == "100755", f"command wrapper is not executable: {rel}")
        if rel.startswith("deploy/") and full.is_file():
            try:
                first_line = full.open(encoding="utf-8").readline().strip()
            except UnicodeDecodeError:
                first_line = ""
            if first_line.startswith("#!"):
                checks.require(mode == "100755", f"deploy script is not executable: {rel}")


def check_json_files(files: dict[str, str], checks: Checks) -> None:
    for rel in files:
        if rel.endswith(".json"):
            load_json(ROOT / rel, checks)


def check_skills(files: dict[str, str], checks: Checks) -> None:
    lock = load_json(ROOT / "workspace/skills.lock.json", checks)
    if not isinstance(lock, dict):
        return
    entries = lock.get("customSkills", [])
    locked = {
        str(entry.get("name")): str(entry.get("path"))
        for entry in entries
        if isinstance(entry, dict)
    }
    discovered = {}
    for rel in files:
        match = re.fullmatch(r"workspace/skills/([^/]+)/SKILL\.md", rel)
        if match:
            discovered[match.group(1)] = str(Path(rel).parent)
    checks.require(locked == discovered, f"skills.lock.json does not match tracked custom skills: locked={locked}, discovered={discovered}")
    for name, rel in locked.items():
        checks.require((ROOT / rel / "SKILL.md").is_file(), f"locked skill is missing SKILL.md: {name} ({rel})")


def check_staged_source(files: dict[str, str], checks: Checks) -> None:
    with tempfile.TemporaryDirectory(prefix="genaebot-stage-") as temp:
        stage = Path(temp) / "live-source"
        stale_files = [
            stage / "workspace/job-scout/jobscout/__pycache__/group_feed.cpython-314.pyc",
            stage / "workspace/lib/__pycache__/genaie_env.cpython-314.pyc",
            stage / "workspace/obsolete-runtime/file.txt",
        ]
        for stale_file in stale_files:
            stale_file.parent.mkdir(parents=True, exist_ok=True)
            stale_file.write_text("stale\n", encoding="utf-8")
        subprocess.run(
            ["bash", str(ROOT / "deploy/stage-openclaw-source.sh"), str(ROOT), str(stage)],
            check=True,
        )
        staged = {
            path.relative_to(stage).as_posix(): path
            for path in stage.rglob("*")
            if path.is_file()
        }
        for stale_file in stale_files:
            checks.require(not stale_file.exists(), f"stale staged file survived restaging: {stale_file.relative_to(stage).as_posix()}")

        for rel, staged_path in staged.items():
            if rel.startswith("workspace/"):
                source_rel = rel
            elif rel.startswith(("agent-workspace-admin/", "agent-workspace-public/")):
                source_rel = f"deploy/{rel}"
            else:
                source_rel = rel
            source = ROOT / source_rel
            checks.require(source_rel in files, f"staged file is not tracked: {rel} (source {source_rel})")
            checks.require(source.is_file(), f"staged file has no source: {rel} (expected {source_rel})")
            if source.is_file():
                checks.require(source.read_bytes() == staged_path.read_bytes(), f"staged file differs from source: {rel}")

        staged_workspace = {rel for rel in staged if rel.startswith("workspace/")}
        tracked_workspace = {rel for rel in files if rel.startswith("workspace/")}
        missing = sorted(tracked_workspace - staged_workspace)
        checks.require(not missing, f"tracked workspace files are not deployable or explicitly exempt: {missing}")

        forbidden_names = {"state.json", "pending-links.json", "subscriptions.json"}
        for rel in staged:
            path = Path(rel)
            checks.require(
                "__pycache__" not in path.parts
                and "node_modules" not in path.parts
                and path.name not in forbidden_names
                and path.suffix not in {".log", ".pyc"},
                f"runtime/cache file reached staged source: {rel}",
            )

        required = {
            "workspace/skills/genaie-calendar/SKILL.md",
            "workspace/docs/public-greeting.txt",
            "workspace/bin/job-scout",
            "workspace/job-scout/job_scout.py",
            "workspace/USER.md",
        }
        checks.require(not (required - staged.keys()), f"required staged files are missing: {sorted(required - staged.keys())}")

        wrapper_pattern = re.compile(r"/home/joseph/\.openclaw/(workspace/[\w./-]+)")
        for rel, path in staged.items():
            if not rel.startswith("workspace/bin/"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for target in wrapper_pattern.findall(text):
                checks.require((stage / target).exists(), f"{rel} references an unstaged target: {target}")


def walk_strings(value: object):
    if isinstance(value, dict):
        for child in value.values():
            yield from walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_strings(child)
    elif isinstance(value, str):
        yield value


def check_runtime_references(checks: Checks) -> None:
    config = load_json(ROOT / "config/openclaw.template.json", checks)
    if not isinstance(config, dict):
        return
    for value in walk_strings(config):
        if value.startswith(RUNTIME_WORKSPACE_PREFIX):
            rel = "workspace/" + value.removeprefix(RUNTIME_WORKSPACE_PREFIX)
            checks.require((ROOT / rel).exists(), f"config references a missing workspace path: {value} ({rel})")

    plugin_paths = config.get("plugins", {}).get("load", {}).get("paths", [])
    for value in plugin_paths:
        rel = "workspace/" + str(value).removeprefix(RUNTIME_WORKSPACE_PREFIX)
        checks.require((ROOT / rel / "openclaw.plugin.json").is_file(), f"plugin load path is invalid: {value}")

    compose = (ROOT / "deploy/docker-compose.override.yml").read_text(encoding="utf-8")
    checks.require(
        "./live-source/workspace/USER.md:/home/joseph/.openclaw/agent-workspace-admin/USER.md:ro" in compose,
        "container boundary does not mount USER.md read-only into the admin workspace",
    )
    checks.require(
        "/home/joseph/.openclaw/agent-workspace-public/MEMORY.md" not in compose
        and "/home/joseph/.openclaw/agent-workspace-public/COMMANDS.md" not in compose,
        "public workspace must not mount MEMORY.md or COMMANDS.md",
    )


LINK_PATTERN = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def check_document_links(files: dict[str, str], checks: Checks) -> None:
    for rel in files:
        if not rel.endswith(".md"):
            continue
        source = ROOT / rel
        text = source.read_text(encoding="utf-8")
        for raw_target in LINK_PATTERN.findall(text):
            target = raw_target.strip().split()[0].strip("<>")
            target = target.split("#", 1)[0]
            if not target or target.startswith(("#", "/", "http://", "https://", "mailto:")):
                continue
            checks.require((source.parent / target).resolve().exists(), f"broken local Markdown link in {rel}: {raw_target}")


def check_service_catalog(checks: Checks) -> None:
    policy = (ROOT / "workspace/docs/public-services.md").read_text(encoding="utf-8").lower()
    prompt = (ROOT / "workspace/docs/public-whatsapp-system-prompt.txt").read_text(encoding="utf-8").lower()
    greeting = (ROOT / "workspace/docs/public-greeting.txt").read_text(encoding="utf-8").lower()
    for label, terms in {
        "Webetu": ("webetu",),
        "personalized news": ("personalized news",),
        "event scouting": ("event scout", "local event"),
        "Job Scout": ("job scout", "job, scholarship"),
    }.items():
        for document_name, document in (("policy", policy), ("prompt", prompt), ("greeting", greeting)):
            checks.require(any(term in document for term in terms), f"{document_name} is missing the {label} service")


def changed_files(base_sha: str | None, head_sha: str) -> set[str]:
    if not base_sha or set(base_sha) == {"0"}:
        return set()
    if subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", f"{base_sha}^{{commit}}"], check=False).returncode:
        return set()
    return set(git("diff", "--name-only", f"{base_sha}...{head_sha}").splitlines())


def documentation_failures(changed: set[str], contract: dict) -> list[str]:
    failures: list[str] = []
    for rule in contract.get("rules", []):
        triggers = {
            path for path in changed
            if any(fnmatch.fnmatchcase(path, pattern) for pattern in rule.get("paths", []))
        }
        required = set(rule.get("requiredDocs", []))
        missing = sorted(required - changed)
        if triggers and missing:
            failures.append(
                f"{rule.get('name', 'unnamed rule')} changed via {sorted(triggers)} but did not update {missing}"
            )
    return failures


def check_documentation_contract(base_sha: str | None, head_sha: str, checks: Checks) -> None:
    if not base_sha:
        print("Documentation diff check skipped: no base SHA supplied")
        return
    contract = load_json(ROOT / "deploy/ci/docs-contract.json", checks)
    if not isinstance(contract, dict):
        return
    changed = changed_files(base_sha, head_sha)
    if not changed:
        print("Documentation diff check skipped: base SHA is unavailable or there are no changes")
        return
    checks.errors.extend(documentation_failures(changed, contract))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-sha")
    parser.add_argument("--head-sha", default="HEAD")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    checks = Checks()
    files = tracked_files()
    check_tracked_policy(files, checks)
    check_json_files(files, checks)
    check_skills(files, checks)
    check_staged_source(files, checks)
    check_runtime_references(checks)
    check_document_links(files, checks)
    check_service_catalog(checks)
    check_documentation_contract(args.base_sha, args.head_sha, checks)
    checks.finish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
