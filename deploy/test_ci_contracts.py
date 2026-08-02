#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import unittest


MODULE_PATH = Path(__file__).parent / "ci" / "check.py"
WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "deploy-main.yml"
NODE_INSTALLS = (
    "npm ci --ignore-scripts --prefix workspace/job-scout",
    "npm ci --ignore-scripts --prefix workspace/job-scout/channel-listener",
)


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
                "name": "job scout",
                "paths": ["workspace/bin/job-scout"],
                "requiredDocs": ["workspace/docs/job-scout-onboarding.md"],
            }]
        }
        failures = ci_check.documentation_failures({"workspace/bin/job-scout"}, contract)
        self.assertEqual(len(failures), 1)

    def test_mapped_contract_change_passes_with_its_document(self) -> None:
        contract = {
            "rules": [{
                "name": "job scout",
                "paths": ["workspace/bin/job-scout"],
                "requiredDocs": ["workspace/docs/job-scout-onboarding.md"],
            }]
        }
        failures = ci_check.documentation_failures(
            {"workspace/bin/job-scout", "workspace/docs/job-scout-onboarding.md"},
            contract,
        )
        self.assertEqual(failures, [])

    def test_internal_change_does_not_require_documentation(self) -> None:
        contract = {
            "rules": [{
                "name": "job scout",
                "paths": ["workspace/bin/job-scout"],
                "requiredDocs": ["workspace/docs/job-scout-onboarding.md"],
            }]
        }
        failures = ci_check.documentation_failures({"workspace/job-scout/job_scout.py"}, contract)
        self.assertEqual(failures, [])


class WorkflowContractTests(unittest.TestCase):
    def assert_installs_precede(self, job: str, marker: str) -> None:
        deployment = job.index(marker)
        for install in NODE_INSTALLS:
            with self.subTest(install=install):
                self.assertIn(install, job)
                self.assertLess(job.index(install), deployment)

    def test_node_dependencies_are_installed_before_repository_tests(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assert_installs_precede(
            job_block(workflow, "validate"), "run: ./deploy/ci-check.sh"
        )

    def test_node_dependencies_are_installed_before_production_deploy(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assert_installs_precede(
            job_block(workflow, "deploy"),
            "run: sudo /usr/local/sbin/openclaw-prod-deploy",
        )

    def test_node_dependencies_are_installed_before_job_scout_deploy(self) -> None:
        # openclaw-prod-deploy re-runs deploy/ci-check.sh on the production
        # host, and its node --test pass imports playwright and baileys from
        # workspace/job-scout. A dispatched Job Scout release must install
        # them exactly like the push-to-main deploy job does.
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assert_installs_precede(
            job_block(workflow, "job-scout-deploy"),
            "sudo /usr/local/sbin/openclaw-prod-deploy",
        )

    def test_memory_preflight_runs_before_self_hosted_dependency_install(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        for name in ("deploy", "job-scout-deploy"):
            with self.subTest(job=name):
                job = job_block(workflow, name)
                preflight = job.index("run: ./deploy/check-deploy-resources.sh")
                self.assertLess(preflight, job.index(NODE_INSTALLS[0]))

    def test_node_cache_tracks_both_package_locks(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("workspace/job-scout/package-lock.json", workflow)
        self.assertIn("workspace/job-scout/channel-listener/package-lock.json", workflow)


if __name__ == "__main__":
    unittest.main()
