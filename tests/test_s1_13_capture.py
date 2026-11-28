"""Fault-injection and independent-maintenance tests for the Staging capture."""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from ops.backup import capture


WORKER = """
import json, os, signal, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'])
print(json.dumps([os.getpid(), child.pid]), flush=True)
time.sleep(90)
"""

WATCHDOG = """
from pathlib import Path
import sys
from ops.backup import capture
class RecoveryProbe(capture.Capture):
    def reopen(self, marker):
        assert marker['status'] == 'Cancelled'
        assert not list(capture.group_members(marker['worker']))
        Path(sys.argv[2]).write_text('reopened after the capture group exited')
        return True
RecoveryProbe(Path(sys.argv[1])).watchdog()
"""


class CaptureMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.config = self.root / "config.json"
        capture.save_json(self.config, {
            "state": str(self.root / "state"),
            "compose_dir": str(Path(__file__).resolve().parents[1]),
            "compose_env": str(self.root / "compose.env"),
            "compose_project": "erp-s113-synthetic",
            "compose_files": [],
            "git_sha": "0" * 40,
            "image_reference": "synthetic-app",
            "minimum_free_bytes": 100,
        })
        self.operation = capture.Capture(self.config)
        self.cut_id = uuid4().hex

    def marker(self, worker, *, expired=False):
        now = monotonic()
        return {
            "cut_id": self.cut_id,
            "boot_id": capture.boot_id(),
            "runner": capture.process_info(os.getpid()),
            "worker": worker,
            "status": "Capturing",
            "cancel_monotonic": now - 1 if expired else now + 120,
            "end_monotonic": now + 180,
        }

    def owned_stage(self):
        stage = self.operation.incomplete / self.cut_id
        stage.mkdir(mode=0o700)
        info = stage.stat()
        capture.save_json(self.operation.attempts / f"{self.cut_id}.json", {
            "cut_id": self.cut_id, "device": info.st_dev, "inode": info.st_ino,
        })
        return stage

    def test_deadline_watchdog_does_not_wait_for_runner_lock_and_stops_group_first(self):
        worker = subprocess.Popen(
            [sys.executable, "-c", WORKER], stdout=subprocess.PIPE, text=True, start_new_session=True,
        )
        try:
            pids = json.loads(worker.stdout.readline())
            identity = capture.process_info(worker.pid)
            self.assertEqual(identity["session"], worker.pid)
            self.assertEqual(
                {member["pid"] for member in capture.group_members(identity)}, set(pids),
            )
            capture.save_json(self.operation.marker, self.marker(identity, expired=True))
            self.operation.reserve.write_bytes(b"reserved")
            observation = self.root / "reopened"
            with capture.locked(self.operation.state / "capture.lock"):
                subprocess.run(
                    [sys.executable, "-c", WATCHDOG, str(self.config), str(observation)],
                    timeout=12, check=True,
                )
                self.assertEqual(observation.read_text(), "reopened after the capture group exited")
                self.assertFalse(self.operation.marker.exists())
            self.assertFalse(self.operation.reserve.exists())
            self.assertFalse(list(self.operation.bundles.iterdir()))
        finally:
            if worker.poll() is None:
                os.killpg(worker.pid, 9)
            worker.wait(timeout=3)
            worker.stdout.close()

    def test_low_capacity_defers_capture_before_inspecting_or_stopping_any_services(self):
        self.operation.compose = Mock()
        with patch.object(capture.shutil, "disk_usage", return_value=Mock(free=99)):
            with self.assertRaises(capture.CaptureError):
                self.operation.prepare(self.cut_id)
        self.assertEqual(self.operation.compose.mock_calls, [])
        self.assertFalse(self.operation.marker.exists())

    def test_enospc_during_attachment_copy_never_finalizes_and_can_reopen(self):
        stage = self.owned_stage()
        identity = capture.process_info(os.getpid())
        marker = self.marker(identity)
        marker.update(image={}, services={"web": {"id": "synthetic-web"}})
        capture.save_json(self.operation.marker, marker)
        compose = self.operation.compose = Mock()
        compose.inspect.return_value = {"State": {"Running": False, "ExitCode": 0}}
        evidence = {
            "postgres": {"server_version_num": 180007},
            "counts": {"test_transactions_attachment": 1},
            "attachments": [{"storage_key": "f" * 32 + ".pdf"}],
        }

        def postgres(command, **kwargs):
            if "pg_dump" in command:
                kwargs["stdout"].write(b"PGDMP synthetic")
                return None
            return json.dumps(evidence).encode()

        compose.postgres.side_effect = postgres
        with patch.object(capture, "copy_attachment", side_effect=OSError(errno.ENOSPC, "synthetic full volume")):
            with self.assertRaises(OSError) as caught:
                self.operation.worker(self.cut_id)
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertFalse((stage / "manifest.json").exists())
        marker["cancel_monotonic"] = monotonic() - 1
        capture.save_json(self.operation.marker, marker)
        with patch.object(capture, "stop_worker") as stop, patch.object(self.operation, "reopen", return_value=True) as reopen:
            self.operation.watchdog()
        self.assertEqual(stop.call_args.args[0]["status"], "Cancelled")
        self.assertEqual(reopen.call_args.args[0]["status"], "Cancelled")
        self.assertFalse(self.operation.marker.exists())
        self.assertFalse(list(self.operation.bundles.iterdir()))
        unowned = self.operation.incomplete / uuid4().hex
        unowned.mkdir(mode=0o700)
        with capture.locked(self.operation.state / "capture.lock"):
            self.operation.clean_incomplete()
        self.assertFalse(stage.exists())
        self.assertTrue(unowned.is_dir())


if __name__ == "__main__":
    unittest.main()
