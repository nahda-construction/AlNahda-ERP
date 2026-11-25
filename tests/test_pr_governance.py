"""Regression tests for trusted-base PR governance checks."""
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
The risk is confined to development tooling; no production data is modified.

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

    def test_schema_and_access_flags(self):
        paths = ["apps/projects/migrations/0003_test.py", "apps/core/access_policy.py"]
        self.assertEqual(gate.validate(body(migration="yes", access="yes"), paths), [])

    def test_financial_flags(self):
        self.assertEqual(gate.validate(body(financial="yes"), ["apps/finance/models.py"]), [])

    def test_old_and_new_rename_paths(self):
        paths = gate.paths_from_api_files([{
            "filename": "docs/archive.md", "previous_filename": "ops/review-pr.sh", "status": "renamed"
        }])
        self.assertEqual(paths, ["docs/archive.md", "ops/review-pr.sh"])
        self.assertTrue(gate.expected_flags(paths)["Governance-sensitive"])

    def test_rename_without_previous_path_fails_closed(self):
        with self.assertRaises(ValueError):
            gate.paths_from_api_files([{"filename": "docs/new.md", "status": "renamed"}])

    def test_missing_risk_narrative_rejected(self):
        text = body().replace("The risk is confined to development tooling; no production data is modified.", "")
        self.assertTrue(any("risk and mitigations" in issue for issue in gate.validate(text, ["README.md"])))

    def test_multiline_html_comment_does_not_satisfy_missing_section(self):
        text = body().replace("Revert the PR commit if necessary.", "<!--\nRevert the PR commit if necessary.\n-->")
        self.assertTrue(any("rollback" in issue for issue in gate.validate(text, ["README.md"])))

    def test_hidden_risk_declarations_not_accepted(self):
        text = body().replace("Governance-sensitive: no", "<!--\nGovernance-sensitive: no\n-->")
        self.assertTrue(any("Governance-sensitive" in issue for issue in gate.validate(text, ["README.md"])))

    def test_unterminated_html_comment_removed(self):
        text = body().replace("Revert the PR commit if necessary.", "<!-- Revert the PR commit if necessary.")
        self.assertTrue(any("rollback" in issue for issue in gate.validate(text, ["README.md"])))

    def test_duplicate_risk_sections_rejected(self):
        text = body() + "\n## Risk and mitigations\nRiskless change\n"
        self.assertTrue(any("Duplicate" in issue for issue in gate.validate(text, ["README.md"])))


    def test_author_can_disclose_risk_missed_by_heuristic(self):
        self.assertEqual(gate.validate(body(access="yes"), ["config/urls.py"]), [])
        self.assertEqual(gate.validate(body(financial="yes"), ["README.md"]), [])

    def test_duplicate_identical_declarations_rejected(self):
        text = body().replace("Governance-sensitive: no", "Governance-sensitive: no\nGovernance-sensitive: no")
        self.assertTrue(any("Duplicate risk declaration: Governance-sensitive" in issue
                            for issue in gate.validate(text, ["README.md"])))

    def test_contradictory_declarations_rejected(self):
        text = body().replace("Governance-sensitive: no", "Governance-sensitive: no\nGovernance-sensitive: yes")
        self.assertTrue(any("Duplicate risk declaration: Governance-sensitive" in issue
                            for issue in gate.validate(text, ["README.md"])))

    def test_invalid_additional_declaration_rejected(self):
        text = body().replace("Governance-sensitive: no", "Governance-sensitive: no\nGovernance-sensitive: maybe")
        self.assertTrue(any("Duplicate risk declaration: Governance-sensitive" in issue
                            for issue in gate.validate(text, ["README.md"])))

    def test_heading_in_fenced_verification_log_ignored(self):
        text = body().replace("Django CI and unit tests.", "Django CI and unit tests.\n\x60\x60\x60text\n## Rollback\nexample\n\x60\x60\x60")
        self.assertEqual(gate.validate(text, ["README.md"]), [])

    def test_missing_rollback_cannot_be_supplied_by_fenced_example(self):
        text = body().replace("## Rollback\nRevert the PR commit if necessary.", "\x60\x60\x60text\n## Rollback\nexample\n\x60\x60\x60")
        self.assertTrue(any("rollback" in issue for issue in gate.validate(text, ["README.md"])))

    def test_fenced_declarations_do_not_count(self):
        text = body().replace("Governance-sensitive: no", "~~~text\nGovernance-sensitive: no\n~~~")
        self.assertTrue(any("Risk declaration is missing: Governance-sensitive" in issue
                            for issue in gate.validate(text, ["README.md"])))

    def test_fence_closes_only_with_matching_marker_and_length(self):
        text = body().replace("Django CI and unit tests.", "Django CI and unit tests.\n~~~~text\n~~~\n## Rollback\n\x60\x60\x60\n~~~~")
        self.assertEqual(gate.validate(text, ["README.md"]), [])

    def test_unclosed_fence_cannot_supply_rollback(self):
        text = body().replace("## Rollback\nRevert the PR commit if necessary.", "~~~\n## Rollback\nRevert the PR commit if necessary.")
        self.assertTrue(any("rollback" in issue for issue in gate.validate(text, ["README.md"])))


if __name__ == "__main__":
    unittest.main()
