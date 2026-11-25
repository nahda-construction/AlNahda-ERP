"""Regression tests for the standalone PR governance checker."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "ci" / "pr_governance.py"
SPEC = importlib.util.spec_from_file_location("pr_governance", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def body(governance="no", migration="no", access="no", financial="no"):
    return f"""## Work items
OPS-AUTO-01

## Authoritative references
AGENTS.md and approved G3 design

## Change summary
Adds safer pull-request governance.

## Verification
Django CI and unit tests.

## Risk and mitigations
Governance-sensitive: {governance}
Schema migration: {migration}
Access/security-sensitive: {access}
Financial-sensitive: {financial}
The changes are isolated to development procedures.

## Rollback
Revert the PR commit if necessary.
"""


class GovernanceTests(unittest.TestCase):
    def test_valid_low_risk(self):
        self.assertEqual(gate.validate(body(), ["README.md"]), [])

    def test_missing_sections_rejected(self):
        issues = gate.validate("## Work items\nOPS-AUTO-01", [])
        self.assertTrue(any("rollback" in message for message in issues))

    def test_governance_change_requires_yes(self):
        issues = gate.validate(body(), [".github/workflows/pr-governance.yml"])
        self.assertTrue(any("Governance-sensitive must be yes" in msg for msg in issues))
        self.assertEqual(gate.validate(body(governance="yes"), [".github/PULL_REQUEST_TEMPLATE.md"]), [])

    def test_schema_and_access_risk_flags(self):
        paths = ["apps/projects/migrations/0003_test.py", "apps/core/access_policy.py"]
        self.assertEqual(
            gate.validate(body(migration="yes", access="yes"), paths),
            [],
        )

    def test_financial_risk_flags(self):
        self.assertEqual(gate.validate(body(financial="yes"), ["apps/finance/models.py"]), [])


if __name__ == "__main__":
    unittest.main()
