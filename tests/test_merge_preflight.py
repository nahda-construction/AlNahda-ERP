"""No-network regression tests for the PR merge preflight rules."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "ops" / "merge_preflight.py"
SPEC = importlib.util.spec_from_file_location("merge_preflight", PATH)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

HEAD = "a" * 40
BASE = "b" * 40


def sample():
    pr = {
        "state": "open", "draft": False, "mergeable": True,
        "base": {"ref": "main", "sha": BASE},
        "head": {"sha": HEAD}, "user": {"login": "author"},
        "body": "Financial-sensitive: no\nAccess/security-sensitive: no",
    }
    main = {"commit": {"sha": BASE}}
    checks = {
        "total_count": 3,
        "check_runs": [
            {
                "id": idx, "name": name, "head_sha": HEAD,
                "app": {"slug": "github-actions"},
                "status": "completed", "conclusion": "success",
            }
            for idx, name in enumerate(gate.REQUIRED_CHECKS, start=1)
        ],
    }
    reviews = [{
        "id": 5, "submitted_at": "2026-11-25T12:00:00Z",
        "user": {"login": "independent-reviewer"}, "state": "APPROVED",
        "commit_id": HEAD,
    }]
    return pr, main, checks, reviews


class PreflightTests(unittest.TestCase):
    def test_valid_review_and_checks(self):
        self.assertEqual(gate.assess(*sample()), [])

    def test_checks_required(self):
        pr, main, checks, reviews = sample()
        checks["check_runs"][1]["conclusion"] = "failure"
        self.assertIn("Required check not successful: PR metadata and risk",
                      gate.assess(pr, main, checks, reviews))

    def test_missing_reviewer(self):
        pr, main, checks, reviews = sample()
        self.assertTrue(any("No fresh APPROVED" in m for m in gate.assess(pr, main, checks, [])))

    def test_self_approval_not_accepted(self):
        pr, main, checks, reviews = sample()
        reviews[0]["user"]["login"] = "author"
        self.assertTrue(any("No fresh APPROVED" in m for m in gate.assess(pr, main, checks, reviews)))

    def test_stale_review_not_accepted(self):
        pr, main, checks, reviews = sample()
        reviews[0]["commit_id"] = "f" * 40
        self.assertTrue(any("No fresh APPROVED" in m for m in gate.assess(pr, main, checks, reviews)))

    def test_request_changes_veto(self):
        pr, main, checks, reviews = sample()
        reviews.append({
            "id": 6, "submitted_at": "2026-11-25T12:01:00Z",
            "user": {"login": "reviewer2"}, "state": "CHANGES_REQUESTED",
            "commit_id": HEAD,
        })
        self.assertTrue(any("requested changes" in m for m in gate.assess(pr, main, checks, reviews)))

    def test_wrong_main_tip(self):
        pr, main, checks, reviews = sample()
        main["commit"]["sha"] = "c" * 40
        self.assertTrue(any("not the current main" in m for m in gate.assess(pr, main, checks, reviews)))

    def test_accounting_requires_separate_fin(self):
        pr, main, checks, reviews = sample()
        pr["body"] = "Financial-sensitive: yes"
        self.assertTrue(any("FIN sign-off" in m for m in gate.assess(pr, main, checks, reviews)))

    def test_bootstrap_interlock_blocks_even_legacy_pass(self):
        pr, main, checks, reviews = sample()
        with patch.object(gate, "get_json", side_effect=[pr, main, checks]), \
             patch.object(gate, "get_reviews", return_value=reviews):
            self.assertEqual(gate.main(["merge_preflight.py", "3"]), 1)


if __name__ == "__main__":
    unittest.main()
