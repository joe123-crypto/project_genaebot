#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).parent / "ci" / "check.py"
WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "deploy-main.yml"
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
    def test_node_dependencies_are_installed_before_repository_tests(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        validation = workflow.index('run: ./deploy/ci-check.sh')
        installs = (
            "npm ci --ignore-scripts --prefix workspace/job-scout",
            "npm ci --ignore-scripts --prefix workspace/job-scout/channel-listener",
        )
        for install in installs:
            with self.subTest(install=install):
                self.assertIn(install, workflow)
                self.assertLess(workflow.index(install), validation)

    def test_node_dependencies_are_installed_before_production_deploy(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        deploy_job = workflow[workflow.index("\n  deploy:"):]
        deployment = deploy_job.index('run: sudo /usr/local/sbin/openclaw-prod-deploy')
        installs = (
            "npm ci --ignore-scripts --prefix workspace/job-scout",
            "npm ci --ignore-scripts --prefix workspace/job-scout/channel-listener",
        )
        for install in installs:
            with self.subTest(install=install):
                self.assertIn(install, deploy_job)
                self.assertLess(deploy_job.index(install), deployment)

    def test_memory_preflight_runs_before_self_hosted_dependency_install(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        deploy_job = workflow[workflow.index("\n  deploy:"):]
        preflight = deploy_job.index("run: ./deploy/check-deploy-resources.sh")
        install = deploy_job.index("npm ci --ignore-scripts --prefix workspace/job-scout")
        self.assertLess(preflight, install)

    def test_node_cache_tracks_both_package_locks(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("workspace/job-scout/package-lock.json", workflow)
        self.assertIn("workspace/job-scout/channel-listener/package-lock.json", workflow)


if __name__ == "__main__":
    unittest.main()
