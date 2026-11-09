#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest


MODULE_PATH = Path(__file__).parent / "ci" / "check.py"
WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "deploy-main.yml"


def job_block(workflow: str, name: str) -> str:
    """Return one top-level job's text, bounded by the next job's key."""
    rest = workflow[workflow.index(f"\n  {name}:\n") + 1 :]
    following = re.search(r"\n  [A-Za-z0-9_-]+:\n", rest)
    return rest[: following.start()] if following else rest
SPEC = importlib.util.spec_from_file_location("ci_check", MODULE_PATH)
assert SPEC and SPEC.loader
ci_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci_check)


class DocumentationContractTests(unittest.TestCase):
    def test_mapped_contract_change_requires_its_document(self) -> None:
        contract = {
            "rules": [{
                "name": "cv maker",
                "paths": ["workspace/bin/cv-maker"],
                "requiredDocs": ["workspace/docs/cv-maker-onboarding.md"],
            }]
        }
        failures = ci_check.documentation_failures({"workspace/bin/cv-maker"}, contract)
        self.assertEqual(len(failures), 1)

    def test_mapped_contract_change_passes_with_its_document(self) -> None:
        contract = {
            "rules": [{
                "name": "cv maker",
                "paths": ["workspace/bin/cv-maker"],
                "requiredDocs": ["workspace/docs/cv-maker-onboarding.md"],
            }]
        }
        failures = ci_check.documentation_failures(
            {"workspace/bin/cv-maker", "workspace/docs/cv-maker-onboarding.md"},
            contract,
        )
        self.assertEqual(failures, [])

    def test_internal_change_does_not_require_documentation(self) -> None:
        contract = {
            "rules": [{
                "name": "cv maker",
                "paths": ["workspace/bin/cv-maker"],
                "requiredDocs": ["workspace/docs/cv-maker-onboarding.md"],
            }]
        }
        failures = ci_check.documentation_failures({"workspace/cv-maker/cvmaker/render.py"}, contract)
        self.assertEqual(failures, [])


class WorkflowContractTests(unittest.TestCase):
    # Repository-plan limitations do not permit branch protection here. The
    # deploy job, rather than a promise about merge discipline, gates production.

    def test_validate_job_runs_the_repository_checks(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("run: ./deploy/ci-check.sh", job_block(workflow, "validate"))

    def test_workflow_names_no_removed_job_scout_paths(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertNotIn("workspace/job-scout", workflow)
        self.assertNotIn("job-scout-browser", workflow)

    def test_production_deploy_requires_all_checks_and_cannot_run_for_prs(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        deploy = job_block(workflow, "deploy")
        self.assertIn("needs: [validate, secrets, compatibility]", deploy)
        self.assertIn("github.ref == 'refs/heads/main'", deploy)
        self.assertIn("github.event_name != 'pull_request'", deploy)
        self.assertIn("cancel-in-progress: false", deploy)
        self.assertIn('--sha "$GITHUB_SHA"', deploy)
        self.assertNotIn("RAILWAY_API_TOKEN", workflow)

    def test_runtime_lane_uses_production_node_and_cannot_silently_skip(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn('node-version: "26.12.1"', job_block(workflow, "validate"))
        compatibility = job_block(workflow, "compatibility")
        self.assertIn("--network none", compatibility)
        self.assertIn("GENAIE_REQUIRE_INTEGRATION=1", compatibility)
        self.assertIn("catalog-recovery.integration.test.mjs", compatibility)
        self.assertIn('= "0"', compatibility)

    def test_normal_and_default_manual_releases_preserve_the_memory_profile(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("default: preserve", workflow)
        self.assertIn("RELEASE_PROFILE: ${{ inputs.profile || 'preserve' }}", job_block(workflow, "deploy"))


class RailwayInstallTests(unittest.TestCase):
    """Execute the workflow's install step with fake network and CLI commands."""

    def run_install(self, failures: int, broken_binary: bool = False):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        step = workflow.split("      - name: Install pinned Railway CLI\n", 1)[1].split("\n      - name:", 1)[0]
        self.assertIn("timeout-minutes: 5", step)
        script = textwrap.dedent(step.split("        run: |\n", 1)[1])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shims = {
                "npm": '''#!/usr/bin/env bash
set -euo pipefail
echo "npm $*" >> "$INSTALL_TRACE"
count=0
if [ -f "$INSTALL_COUNT" ]; then read -r count < "$INSTALL_COUNT"; fi
count=$((count + 1))
echo "$count" > "$INSTALL_COUNT"
if [ "$count" -le "$INSTALL_FAILURES" ]; then
  echo "Failed fetching the binary (504 Gateway Time-out)" >&2
  exit 1
fi
''',
                "railway": '''#!/usr/bin/env bash
echo "railway $*" >> "$INSTALL_TRACE"
if [ "$INSTALL_BROKEN_BINARY" = 1 ]; then exit 1; fi
echo "railway 5.71.0"
''',
                "sleep": '''#!/usr/bin/env bash
echo "sleep $*" >> "$INSTALL_TRACE"
''',
            }
            for name, body in shims.items():
                path = root / name
                path.write_text(body)
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"],
                       INSTALL_TRACE=str(root / "trace"), INSTALL_COUNT=str(root / "count"),
                       INSTALL_FAILURES=str(failures), INSTALL_BROKEN_BINARY=str(int(broken_binary)))
            result = subprocess.run(["bash", "-euo", "pipefail", "-c", script], env=env,
                                    text=True, capture_output=True, timeout=10)
            return result, (root / "trace").read_text().splitlines()

    def test_success_does_not_retry(self):
        result, trace = self.run_install(0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(trace, ["npm install --global @railway/cli@5.71.0", "railway --version"])

    def test_transient_binary_download_failure_recovers_with_backoff(self):
        result, trace = self.run_install(2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(trace, ["npm install --global @railway/cli@5.71.0", "sleep 10",
                                 "npm install --global @railway/cli@5.71.0", "sleep 20",
                                 "npm install --global @railway/cli@5.71.0", "railway --version"])

    def test_exhausted_downloads_fail_before_release(self):
        result, trace = self.run_install(3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("::error::", result.stdout)
        self.assertEqual(sum(line.startswith("npm ") for line in trace), 3)
        self.assertNotIn("railway --version", trace)
        self.assertEqual([line for line in trace if line.startswith("sleep ")], ["sleep 10", "sleep 20"])

    def test_successful_npm_install_with_a_broken_binary_still_fails(self):
        result, trace = self.run_install(0, broken_binary=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(trace.count("railway --version"), 3)


if __name__ == "__main__":
    unittest.main()
