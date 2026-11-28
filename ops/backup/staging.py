"""Connect the Staging capture and transport tools to the host scheduler."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from transport import BackupError, Queue, Restic


def handoff(queue, bundles):
    errors = 0
    if not bundles.exists():
        return errors
    for bundle in sorted(bundles.iterdir()):
        manifest_path = bundle / "manifest.json"
        if bundle.is_symlink() or manifest_path.is_symlink() or not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_bytes())
            if manifest.get("capture_state") != "Finalized" or manifest.get("cut_id") != bundle.name:
                raise BackupError("Backup handoff identity could not be verified")
            job = queue.capture(bundle)
            if job["state"] in {"Pending", "Verified"}:
                # Remove only a finalized copy confirmed in the encrypted repository.
                shutil.rmtree(bundle)
                print(f"S1-13: queued encrypted snapshot for {job['job_id']}", flush=True)
        except (BackupError, ValueError, OSError, KeyError) as error:
            errors += 1
            message = str(error) if isinstance(error, BackupError) else type(error).__name__
            print(f"S1-13: one capture could not be handed off: {message}", file=sys.stderr)
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("action", choices=("capture", "transfer"))
    args = parser.parse_args()
    cfg = json.loads(args.config.read_bytes())
    state = Path(cfg["state"])
    if not state.is_absolute() or state.resolve() != state:
        raise BackupError("The backup state directory must be an absolute canonical path")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.stat().st_uid != os.getuid() or state.stat().st_mode & 0o077:
        raise BackupError("The backup state must be private to the configured service user")
    source = Restic(os.environ.get("RESTIC_REPOSITORY"), os.environ.get("RESTIC_PASSWORD_FILE"))
    destination = None
    if args.action == "transfer":
        destination = Restic(os.environ.get("S1_13_DEST_REPOSITORY"), os.environ.get("S1_13_DEST_PASSWORD_FILE"))
    queue = Queue(state / "transport", source, destination)
    if args.action == "capture":
        script = Path(__file__).with_name("capture.py")
        subprocess.run([sys.executable, str(script), "--config", str(args.config), "capture"], check=True)
    with (state / "handoff.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("S1-13: another scheduled operation owns the handoff; retry at the next interval")
            return 0
        errors = handoff(queue, state / "bundles")
    if args.action == "capture":
        return bool(errors)
    errors += queue.transfer()
    status = queue.status()
    if status["rpo_alert"]:
        print("S1-13 RPO alert: the latest verified external cutoff is absent or older than 24 hours", file=sys.stderr)
    print(json.dumps(status))
    return bool(errors)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BackupError, ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        message = str(error) if isinstance(error, BackupError) else type(error).__name__
        raise SystemExit(f"S1-13: {message}") from None
