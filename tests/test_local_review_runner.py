"""No-network tests for local advisory runner trust and deduplication."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
import os
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "ops" / "local_review_runner.py"
SPEC = importlib.util.spec_from_file_location("local_review_runner", PATH)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def pr():
    return {"number": 3, "state": "open", "draft": False, "body": "sample",
            "head": {"sha": "a" * 40, "repo": {"full_name": runner.REPO}},
            "base": {"sha": "b" * 40, "ref": "main", "repo": {"full_name": runner.REPO}}}


class RunnerTests(unittest.TestCase):
    def test_snapshot_changes_with_body(self):
        one = pr()
        two = dict(one, body="changed")
        self.assertNotEqual(runner.snapshot(one, 3), runner.snapshot(two, 3))

    def test_snapshot_rejects_draft(self):
        with self.assertRaises(ValueError):
            runner.snapshot(dict(pr(), draft=True), 3)

    def test_snapshot_rejects_fork(self):
        one = pr()
        one["head"]["repo"]["full_name"] = "unknown/fork"
        with self.assertRaises(ValueError):
            runner.snapshot(one, 3)

    def test_snapshot_rejects_invalid_head(self):
        one = pr()
        one["head"]["sha"] = "main"
        with self.assertRaises(ValueError):
            runner.snapshot(one, 3)

    def test_snapshot_rejects_wrong_number(self):
        with self.assertRaises(ValueError):
            runner.snapshot(pr(), 5)

    def test_report_uses_safe_long_fence_and_advisory_label(self):
        rev = runner.snapshot(pr(), 3)
        report = runner.build_report(3, rev, "`````\n@user\nreview")
        self.assertTrue(report.startswith(runner.marker(3, rev) + "\n"))
        self.assertIn("``````text", report)
        self.assertIn("Not a GitHub APPROVED review", report)

    def test_empty_and_control_character_output_rejected(self):
        for text in ("", "   ", "test\x00", "x" * (runner.MAX_REPORT + 1)):
            with self.subTest(text=text[:10]), self.assertRaises(ValueError):
                runner.build_report(3, runner.snapshot(pr(), 3), text)

    def test_deduplication_checks_comment_author(self):
        tag = runner.marker(3, runner.snapshot(pr(), 3))
        comment = {"user": {"login": "other"}, "body": tag + "\nreview"}
        with patch.object(runner, "api", return_value=[comment]):
            self.assertFalse(runner.comments_have_marker(3, tag, "owner"))
        with patch.object(runner, "api", return_value=[dict(comment, user={"login": "owner"})]):
            self.assertTrue(runner.comments_have_marker(3, tag, "owner"))

    def test_already_reviewed_skips_model(self):
        with patch.object(runner, "api", return_value=pr()), \
             patch.object(runner, "comments_have_marker", return_value=True), \
             patch.object(runner, "review") as model:
            self.assertEqual(runner.review_one(Path("/tmp"), 3, "owner", True), "already reviewed")
            model.assert_not_called()

    def test_stale_revision_never_posts(self):
        with patch.object(runner, "api", side_effect=[pr(), dict(pr(), body="changed")]), \
             patch.object(runner, "comments_have_marker", return_value=False), \
             patch.object(runner, "diff_for", return_value="diff"), \
             patch.object(runner, "review", return_value="finding"), \
             patch.object(runner, "run") as process:
            with self.assertRaisesRegex(ValueError, "changed during review"):
                runner.review_one(Path("/tmp"), 3, "owner", True)
            process.assert_not_called()

    def test_success_posts_only_advisory_comment(self):
        with patch.object(runner, "api", return_value=pr()), \
             patch.object(runner, "comments_have_marker", return_value=False), \
             patch.object(runner, "diff_for", return_value="diff"), \
             patch.object(runner, "review", return_value="finding"), \
             patch.object(runner, "run") as process:
            self.assertEqual(runner.review_one(Path("/tmp"), 3, "owner", True),
                             "advisory comment posted")
            process.assert_called_once()
            args = process.call_args.args[0]
            self.assertEqual(args[:4], ["gh", "pr", "comment", "3"])
            self.assertIn("Not a GitHub APPROVED review", process.call_args.kwargs["input_text"])

    def test_base_fetch_mismatch_blocks_diff(self):
        with patch.object(runner, "git", side_effect=["", "c" * 40]) as git:
            with self.assertRaisesRegex(ValueError, "Base moved"):
                runner.diff_for(Path("/tmp"), 3, runner.snapshot(pr(), 3))
            self.assertEqual(git.call_count, 2)

    def test_head_fetch_mismatch_blocks_diff(self):
        with patch.object(runner, "git", side_effect=["", "b" * 40, "", "c" * 40]):
            with self.assertRaisesRegex(ValueError, "Head moved"):
                runner.diff_for(Path("/tmp"), 3, runner.snapshot(pr(), 3))

    def test_oversized_diff_blocks_review(self):
        with patch.object(runner, "git", side_effect=["", "b" * 40, "", "a" * 40,
                                                     "x" * (runner.MAX_DIFF + 1)]):
            with self.assertRaisesRegex(ValueError, "oversized"):
                runner.diff_for(Path("/tmp"), 3, runner.snapshot(pr(), 3))

    def test_model_call_disables_tools_and_excludes_credentials(self):
        with patch.dict(os.environ, {"GH_TOKEN": "fake-gh", "OPENAI_API_KEY": "fake-api",
                                     "DB_PASSWORD": "fake-db", "PATH": "/usr/bin"}), \
             patch.object(runner, "run", return_value="review") as process:
            self.assertEqual(runner.review("test diff"), "review")
            args = process.call_args.args[0]
            for value in ("--ignore-user-config", "--ignore-rules", "--ephemeral", "read-only", "never",
                          "features.apps=false", "features.hooks=false",
                          "features.multi_agent=false", "features.memories=false",
                          "features.shell_tool=false", "mcp_servers={}",
                          'web_search="disabled"', 'forced_login_method="chatgpt"'):
                self.assertIn(value, args)
            env = process.call_args.kwargs["env"]
            for secret in ("GH_TOKEN", "OPENAI_API_KEY", "DB_PASSWORD"):
                self.assertNotIn(secret, env)
            self.assertIn("test diff", process.call_args.kwargs["input_text"])

    def test_pilot_rejects_unapproved_pr_before_git_access(self):
        with patch("sys.argv", ["runner", "--repo", "/tmp", "--pr", "99", "--once"]), \
             patch.object(runner, "git") as git:
            with self.assertRaises(SystemExit) as ctx:
                runner.main()
            self.assertEqual(ctx.exception.code, 2)
            git.assert_not_called()


if __name__ == "__main__":
    unittest.main()
