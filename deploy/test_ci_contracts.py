#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).parent / "ci" / "check.py"
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


if __name__ == "__main__":
    unittest.main()
