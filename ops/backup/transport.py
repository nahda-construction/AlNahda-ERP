"""Preserve a durable queue of Staging backup snapshots and verify their delivery."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile


class BackupError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc)


def parse_time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise BackupError("Cutoff must have an explicit time zone")
    return parsed.astimezone(timezone.utc)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical_manifest(data):
    manifest = json.loads(data)
    identifier = manifest["cut_id"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", identifier):
        raise BackupError("Invalid cut identifier")
    parse_time(manifest["cut_at"])
    if manifest.get("capture_state") != "Finalized":
        raise BackupError("Cancelled or unfinished captures cannot be verified")
    files = manifest["files"]
    if not isinstance(files, dict) or not files:
        raise BackupError("Empty or invalid manifest")
    for name, fingerprint in files.items():
        path = PurePosixPath(name)
        if not path.parts or path.is_absolute() or ".." in path.parts or str(path) != name:
            raise BackupError("Unsafe manifest path")
        if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise BackupError("Invalid manifest fingerprint")
    return manifest


class Restic:
    def __init__(self, repository, password_file):
        if not repository or not password_file:
            raise BackupError("Missing Restic repository or password-file binding")
        self.repository = repository
        self.password_file = password_file

    def run(self, *args, cwd=None, json_output=False):
        env = os.environ.copy()
        for key in ("RESTIC_PASSWORD", "RESTIC_PASSWORD_COMMAND"):
            env.pop(key, None)
        env.update(RESTIC_REPOSITORY=self.repository, RESTIC_PASSWORD_FILE=str(self.password_file))
        command = ["restic", *args]
        if json_output:
            command.append("--json")
        try:
            result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, timeout=3600, check=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise BackupError(f"Restic {args[0]} did not complete") from exc
        return result.stdout

    def snapshots(self, tag):
        return json.loads(self.run("snapshots", "--tag", tag, json_output=True)) or []

    def identity(self):
        return json.loads(self.run("cat", "config"))["id"]

    def snapshot_tree(self, snapshot):
        nodes = {}
        for line in self.run("ls", snapshot, json_output=True).splitlines():
            record = json.loads(line)
            if record.get("type") == "file" and "path" in record:
                nodes[record["path"]] = record
        return nodes

    def verify(self, snapshot, manifest_digest, *, full):
        nodes = self.snapshot_tree(snapshot)
        candidates = [path for path in nodes if PurePosixPath(path).name == "manifest.json"]
        if len(candidates) != 1:
            raise BackupError("Expected one readable backup manifest")
        path = candidates[0]
        source = self.run("dump", snapshot, path)
        if digest(source) != manifest_digest:
            raise BackupError("Snapshot manifest does not match the captured bundle")
        manifest = canonical_manifest(source)
        root = PurePosixPath(path).parent
        for relative, fingerprint in manifest["files"].items():
            pathname = str(root / relative)
            if pathname not in nodes:
                raise BackupError("Snapshot file is absent")
            if full and digest(self.run("dump", snapshot, pathname)) != fingerprint:
                raise BackupError("Snapshot file contents do not match the manifest")
        self.run("check", *(("--read-data",) if full else ()))
        return manifest


class Queue:
    def __init__(self, state, source, destination, *, synthetic_verify=False):
        state.mkdir(parents=True, mode=0o700, exist_ok=True)
        if state.is_symlink() or state.stat().st_uid != os.getuid() or state.stat().st_mode & 0o077:
            raise BackupError("Backup queue directory must be private and owned by its operator")
        self.state = state
        self.source = source
        self.destination = destination
        self.synthetic_verify = synthetic_verify
        self.queue_file = state / "queue.json"

    @contextmanager
    def locked(self):
        with (self.state / "queue.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.queue_file.exists():
                data = json.loads(self.queue_file.read_bytes())
            else:
                data = {"jobs": []}
            yield data

    def save(self, data):
        with tempfile.NamedTemporaryFile(mode="w", dir=self.state, delete=False) as stream:
            try:
                json.dump(data, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
                os.replace(stream.name, self.queue_file)
                fd = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            finally:
                Path(stream.name).unlink(missing_ok=True)

    def finish_capture(self, data, job):
        matching = self.source.snapshots(job["tag"])
        if not matching:
            bundle = Path(job["bundle"])
            self.inspect_bundle(bundle, job["manifest_sha256"])
            self.source.run("backup", "--tag", job["tag"], ".", cwd=bundle)
            matching = self.source.snapshots(job["tag"])
        if len(matching) != 1:
            raise BackupError("Expected one local snapshot for the cut")
        snapshot = matching[0]["id"]
        self.source.verify(snapshot, job["manifest_sha256"], full=self.synthetic_verify)
        job.update(state="Pending", source_snapshot_id=snapshot, next_attempt_at=now().isoformat())
        self.save(data)

    @staticmethod
    def inspect_bundle(bundle, expected=None):
        manifest_file = bundle / "manifest.json"
        if not bundle.is_absolute() or bundle.resolve() != bundle or not bundle.is_dir() or manifest_file.is_symlink():
            raise BackupError("Bundle must be a canonical, finalized directory")
        source = manifest_file.read_bytes()
        if expected and digest(source) != expected:
            raise BackupError("Bundle differs from recorded capture")
        manifest = canonical_manifest(source)
        for name, fingerprint in manifest["files"].items():
            file = bundle / name
            if file.resolve() != file or not file.is_file():
                raise BackupError("Bundle contains an absent or unsafe file")
            with file.open("rb") as reader:
                if hashlib.file_digest(reader, "sha256").hexdigest() != fingerprint:
                    raise BackupError("Bundle file differs from manifest")
        return manifest, digest(source)

    def capture(self, bundle):
        manifest, sha = self.inspect_bundle(bundle)
        identifier = manifest["cut_id"]
        with self.locked() as data:
            job = next((item for item in data["jobs"] if item["cut_id"] == identifier), None)
            if job and job["manifest_sha256"] != sha:
                raise BackupError("Cut identifier already belongs to different content")
            if not job:
                job = {
                    "cut_id": identifier, "cut_at": parse_time(manifest["cut_at"]).isoformat(),
                    "tag": f"s1-13:{identifier}", "job_id": f"s1-13:{identifier}", "bundle": str(bundle),
                    "manifest_sha256": sha, "state": "Capturing", "attempts": 0,
                }
                data["jobs"].append(job)
                self.save(data)
            if job["state"] == "Capturing":
                self.finish_capture(data, job)
            return job

    def transfer(self):
        if self.destination is None or self.source.repository == self.destination.repository:
            raise BackupError("A separate destination repository is required")
        errors = 0
        checked_identity = False
        with self.locked() as data:
            jobs = sorted(data["jobs"], key=lambda job: parse_time(job["cut_at"]))
            for job in jobs:
                if job["state"] == "Verified":
                    continue
                if job.get("next_attempt_at") and parse_time(job["next_attempt_at"]) > now():
                    continue
                try:
                    if job["state"] == "Capturing":
                        self.finish_capture(data, job)
                    if not checked_identity:
                        if self.source.identity() == self.destination.identity():
                            raise BackupError("Source and destination resolve to the same Restic repository")
                        checked_identity = True
                    snapshots = self.destination.snapshots(job["tag"])
                    if not snapshots:
                        self.destination.run(
                            "copy", "--from-repo", self.source.repository, "--from-password-file",
                            str(self.source.password_file), job["source_snapshot_id"],
                        )
                        snapshots = self.destination.snapshots(job["tag"])
                    if len(snapshots) != 1:
                        raise BackupError("Expected one destination snapshot for the cut")
                    snapshot = snapshots[0]["id"]
                    self.destination.verify(snapshot, job["manifest_sha256"], full=self.synthetic_verify)
                    job.update(state="Verified", destination_snapshot_id=snapshot, verified_at=now().isoformat())
                    job.pop("last_error", None)
                except (BackupError, OSError) as exc:
                    errors += 1
                    job["attempts"] += 1
                    delay = min(60, 5 * (2 ** min(4, job["attempts"] - 1)))
                    job["next_attempt_at"] = (now() + timedelta(minutes=delay)).isoformat()
                    job["last_error"] = str(exc) if isinstance(exc, BackupError) else type(exc).__name__
                self.save(data)
        return errors

    def status(self):
        with self.locked() as data:
            latest = max(
                (parse_time(job["cut_at"]) for job in data["jobs"] if job["state"] == "Verified"),
                default=None,
            )
            return {
                "latest_verified_cut_at": latest.isoformat() if latest else None,
                "rpo_alert": latest is None or now() - latest > timedelta(hours=24),
                "jobs": [{key: val for key, val in job.items() if key != "bundle"} for job in data["jobs"]],
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=os.environ.get("S1_13_STATE"))
    parser.add_argument("--synthetic-verify", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("capture").add_argument("bundle", type=Path)
    commands.add_parser("transfer")
    commands.add_parser("status")
    args = parser.parse_args()
    if args.state is None:
        parser.error("set --state or S1_13_STATE")

    source = Restic(os.environ.get("RESTIC_REPOSITORY"), os.environ.get("RESTIC_PASSWORD_FILE"))
    destination = None
    if args.command == "transfer":
        destination = Restic(os.environ.get("S1_13_DEST_REPOSITORY"), os.environ.get("S1_13_DEST_PASSWORD_FILE"))
    queue = Queue(args.state, source, destination, synthetic_verify=args.synthetic_verify)
    if args.command == "capture":
        job = queue.capture(args.bundle)
        print(json.dumps({key: value for key, value in job.items() if key != "bundle"}))
    elif args.command == "transfer":
        errors = queue.transfer()
        print(json.dumps(queue.status()))
        return bool(errors)
    else:
        print(json.dumps(queue.status()))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BackupError, ValueError, KeyError, OSError) as error:
        message = str(error) if isinstance(error, BackupError) else type(error).__name__
        raise SystemExit(f"S1-13: {message}") from None
