"""No-network tests for the fail-closed local preparation stage."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
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
        self.assertNotEqual(runner.snapshot(pr(), 3),
                            runner.snapshot(dict(pr(), body="changed"), 3))

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

    def test_output_limit_enforced_during_execution(self):
        with self.assertRaisesRegex(ValueError, "byte limit"):
            runner.run_bytes([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x'*1000000)"],
                             max_output=1024, timeout=5)

    def test_stderr_limit(self):
        with patch.object(runner, "MAX_STDERR", 1024):
            with self.assertRaisesRegex(ValueError, "byte limit"):
                runner.run_bytes([sys.executable, "-c", "import sys; sys.stderr.buffer.write(b'x'*1000000)"],
                                 timeout=5)

    def test_timeout(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            runner.run_bytes([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.1)

    def test_raw_output_preserves_invalid_bytes_and_crlf(self):
        raw = runner.run_bytes([sys.executable, "-c",
                                "import sys; sys.stdout.buffer.write(bytes([255,13,10,92,114]))"])
        self.assertEqual(raw, bytes([255,13,10,92,114]))
        self.assertEqual(runner.visible_diff(raw), r"\xff\r" + "\n" + r"\\r")

    def test_api_pins_github_hostname(self):
        with patch.object(runner, "run", return_value="{}") as run:
            self.assertEqual(runner.api("user"), {})
            self.assertEqual(run.call_args.args[0],
                             ["gh", "api", "--hostname", "github.com", "-X", "GET", "user"])

    def test_base_fetch_mismatch_is_retryable(self):
        with patch.object(runner, "check_disk"), \
             patch.object(runner, "git", side_effect=["", "c" * 40]):
            with self.assertRaisesRegex(runner.RevisionChanged, "Base moved"):
                runner.diff_for(Path("/tmp"), 3, runner.snapshot(pr(), 3))

    def test_head_fetch_mismatch_is_retryable(self):
        with patch.object(runner, "check_disk"), \
             patch.object(runner, "git", side_effect=["", "b" * 40, "", "c" * 40]):
            with self.assertRaisesRegex(runner.RevisionChanged, "Head moved"):
                runner.diff_for(Path("/tmp"), 3, runner.snapshot(pr(), 3))

    def test_binary_change_is_blocked(self):
        with patch.object(runner, "check_disk"), \
             patch.object(runner, "git", side_effect=["", "b" * 40, "", "a" * 40]), \
             patch.object(runner, "git_bytes", return_value=b"-\t-\tbinary.dat\0"):
            with self.assertRaisesRegex(ValueError, "Binary"):
                runner.diff_for(Path("/tmp"), 3, runner.snapshot(pr(), 3))

    def test_oversized_diff_blocked(self):
        with patch.object(runner, "check_disk"), \
             patch.object(runner, "git", side_effect=["", "b" * 40, "", "a" * 40]), \
             patch.object(runner, "git_bytes", side_effect=[b"1\t1\tfile\0", b"x"*(runner.MAX_DIFF+1)]):
            with self.assertRaisesRegex(ValueError, "oversized"):
                runner.diff_for(Path("/tmp"), 3, runner.snapshot(pr(), 3))

    def test_stale_revision_never_prepares(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(runner, "api", side_effect=[pr(), dict(pr(), body="changed")]), \
             patch.object(runner, "diff_for", return_value=b"diff"):
            with self.assertRaisesRegex(runner.RevisionChanged, "changed during"):
                runner.prepare_one(Path("/tmp"), 3, Path(tmp))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_local_candidate_only_and_dedup(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(runner, "api", return_value=pr()), \
             patch.object(runner, "diff_for", return_value=b"diff\r\n\xff") as diff:
            self.assertIn("model review blocked", runner.prepare_one(Path("/tmp"), 3, Path(tmp)))
            self.assertEqual(next(Path(tmp).glob("*.diff")).read_bytes(), b"diff\r\n\xff")
            self.assertEqual(runner.prepare_one(Path("/tmp"), 3, Path(tmp)), "already prepared")
            diff.assert_called_once()

    def test_publish_and_polling_block_before_git_access(self):
        for flags in (["--once", "--publish"], [], ["--publish"]):
            with self.subTest(flags=flags), \
                 patch("sys.argv", ["runner", "--repo", "/tmp", "--pr", "3", *flags]), \
                 patch.object(runner, "git") as git:
                with self.assertRaises(SystemExit) as ctx:
                    runner.main()
                self.assertEqual(ctx.exception.code, 2)
                git.assert_not_called()

    def test_pilot_rejects_unapproved_pr_before_git_access(self):
        with patch("sys.argv", ["runner", "--repo", "/tmp", "--pr", "99", "--once"]), \
             patch.object(runner, "git") as git:
            with self.assertRaises(SystemExit) as ctx:
                runner.main()
            self.assertEqual(ctx.exception.code, 2)
            git.assert_not_called()


if __name__ == "__main__":
    unittest.main()
