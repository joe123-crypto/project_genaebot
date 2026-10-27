#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
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


if __name__ == "__main__":
    unittest.main()
