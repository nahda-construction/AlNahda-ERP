"""Capture a consistent Staging database and its committed private attachments."""

import argparse
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
from time import monotonic, sleep
from uuid import uuid4


DRAIN_SECONDS = 60
CANCEL_SECONDS = 8 * 60
WINDOW_SECONDS = 10 * 60
RAW_MAX_AGE_SECONDS = 24 * 60 * 60
KEY_PATTERN = re.compile(r"[0-9a-f]{32}\.pdf")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")

DATABASE_EVIDENCE_SQL = r"""
CREATE TEMP TABLE backup_counts (table_name text, row_count bigint);
SELECT format('INSERT INTO backup_counts SELECT %L, count(*) FROM %I.%I', tablename, schemaname, tablename)
FROM pg_catalog.pg_tables WHERE schemaname = 'public' ORDER BY tablename
\gexec
SELECT jsonb_build_object(
    'postgres', jsonb_build_object(
        'server_version', current_setting('server_version'),
        'server_version_num', current_setting('server_version_num')::integer,
        'extensions', (SELECT jsonb_object_agg(extname, extversion) FROM pg_catalog.pg_extension)),
    'counts', (SELECT jsonb_object_agg(table_name, row_count) FROM backup_counts),
    'migrations', (SELECT jsonb_agg(jsonb_build_array(app, name) ORDER BY app, name) FROM public.django_migrations),
    'control_totals', jsonb_build_object(
        'test_transaction_amount', (SELECT coalesce(sum(amount), 0)::text FROM public.test_transactions_testtransactionline),
        'stock_balance_quantity', (SELECT coalesce(sum(quantity), 0)::text FROM public.inventory_stocklocationbalance),
        'stock_movement_signed_quantity', (SELECT coalesce(sum(signed_qty), 0)::text FROM public.inventory_stockmovement)),
    'attachments', (SELECT coalesce(jsonb_agg(jsonb_build_object('storage_key', storage_key,
        'size_bytes', size_bytes, 'sha256', sha256) ORDER BY storage_key), '[]'::jsonb)
        FROM public.test_transactions_attachment));
"""


class CaptureError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def boot_id():
    return BOOT_ID_PATH.read_text().strip()


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save_json(path, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
            fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


def read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CaptureError("The backup journal or configuration is not private")
        return json.load(stream)


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not path.is_absolute() or path.resolve() != path or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid() or info.st_mode & 0o077
    ):
        raise CaptureError("Backup state must be a private owned directory outside the checkout")
    return path


@contextmanager
def locked(path, *, wait=True):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "r+b"):
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CaptureError("Invalid backup lock")
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        yield


def process_info(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": int(pid), "state": fields[0], "group": int(fields[2]), "session": int(fields[3]), "birth": int(fields[19])}
    except (FileNotFoundError, ProcessLookupError):
        return None


def same_process(identity):
    current = process_info(identity["pid"])
    return current and current["birth"] == identity["birth"] and current["state"] not in ("Z", "X")


def group_members(identity):
    for path in Path("/proc").iterdir():
        if not path.name.isdecimal():
            continue
        info = process_info(path.name)
        if (
            info and info["session"] == identity["pid"] and info["group"] == identity["pid"]
            and info["birth"] >= identity["birth"] and info["state"] not in ("Z", "X")
        ):
            yield info


def stop_worker(marker):
    if marker["boot_id"] != boot_id():
        return
    identity = marker["worker"]
    leader = process_info(identity["pid"])
    if leader and leader["birth"] != identity["birth"]:
        return
    for signum, grace in ((signal.SIGTERM, 2), (signal.SIGKILL, 5)):
        deadline = monotonic() + grace
        while list(group_members(identity)) and monotonic() < deadline:
            with suppress(ProcessLookupError):
                os.killpg(identity["pid"], signum)
            sleep(0.1)
    if list(group_members(identity)):
        raise CaptureError("Capture process group did not stop; writing remains disabled")


class Compose:
    def __init__(self, config):
        self.directory = Path(config["compose_dir"])
        self.project = config["compose_project"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]+", self.project):
            raise CaptureError("Invalid Compose project name")
        self.command = ["docker", "compose", "--project-directory", str(self.directory),
                        "--env-file", config["compose_env"], "-p", self.project]
        for file in config["compose_files"]:
            self.command.extend(("-f", file))

    @staticmethod
    def docker(*args, input=None, stdout=subprocess.PIPE, timeout=15):
        try:
            return subprocess.run(
                ["docker", *args], input=input, stdout=stdout, stderr=subprocess.PIPE,
                timeout=timeout, check=True,
            ).stdout
        except (subprocess.SubprocessError, OSError) as exc:
            raise CaptureError("Docker operation failed") from exc

    def run(self, *args, input=None, stdout=subprocess.PIPE, timeout=15):
        return self.docker(*self.command[1:], *args, input=input, stdout=stdout, timeout=timeout)

    def inspect(self, container):
        return json.loads(self.docker("inspect", "--type", "container", container))[0]

    def container(self, service):
        ids = self.run("ps", "-a", "-q", service).decode().split()
        if len(ids) != 1 or not SHA256_PATTERN.fullmatch(ids[0]):
            raise CaptureError(f"Expected exactly one Compose {service} container")
        data = self.inspect(ids[0])
        labels = data["Config"]["Labels"]
        if labels.get("com.docker.compose.project") != self.project or labels.get("com.docker.compose.service") != service:
            raise CaptureError("Container identity differs from the Staging project")
        return data

    def postgres(self, script, *, input=None, stdout=subprocess.PIPE, timeout=120, application_name=None):
        arguments = ["exec", "-T"]
        if application_name is not None:
            arguments.extend(("-e", f"PGAPPNAME={application_name}"))
        return self.run(*arguments, "db", "sh", "-ec", script, input=input, stdout=stdout, timeout=timeout)


def file_sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy_attachment(container, attachment, output):
    key = attachment["storage_key"]
    if not KEY_PATTERN.fullmatch(key) or not SHA256_PATTERN.fullmatch(attachment["sha256"]):
        raise CaptureError("Invalid committed attachment reference")
    command = ["docker", "cp", f"{container}:/var/lib/erp/private/{key}", "-"]
    digest = hashlib.sha256()
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                member = archive.next()
                if (
                    member is None or not member.isfile() or member.name != key
                    or member.size != attachment["size_bytes"] or member.size <= 0
                ):
                    raise CaptureError("Missing attachment or incorrect file size")
                with archive.extractfile(member) as reader, output.open("xb") as writer:
                    for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                        digest.update(chunk)
                        writer.write(chunk)
                    writer.flush()
                    os.fsync(writer.fileno())
                if archive.next() is not None or digest.hexdigest() != attachment["sha256"]:
                    raise CaptureError("Attachment differs from the committed fingerprint")
            if process.wait(timeout=10):
                raise CaptureError("Could not read committed attachment")
        finally:
            if process.poll() is None:
                process.kill()


class Capture:
    def __init__(self, config_path):
        self.config_path = config_path.resolve(strict=True)
        self.config = read_json(self.config_path)
        self.compose = Compose(self.config)
        self.state = Path(self.config["state"])
        if self.state.resolve().is_relative_to(self.compose.directory.resolve()):
            raise CaptureError("Backup state must be outside the checkout")
        private_directory(self.state)
        self.incomplete = private_directory(self.state / "capture-incomplete")
        self.bundles = private_directory(self.state / "bundles")
        self.attempts = private_directory(self.state / "capture-attempts")
        self.marker = self.state / "maintenance.json"
        self.reserve = self.state / "maintenance.reserve"

    def read_marker(self):
        try:
            return read_json(self.marker)
        except FileNotFoundError:
            return None

    def prepare(self, cut_id):
        have = shutil.disk_usage(self.state).free
        minimum = self.config["minimum_free_bytes"]
        if not isinstance(minimum, int) or minimum <= 0 or have < minimum:
            raise CaptureError("Insufficient configured free space for capture; writing remains available")

        services = {name: self.compose.container(name) for name in ("db", "printer", "web")}
        for name in ("db", "web"):
            state = services[name]["State"]
            if not state["Running"] or state.get("Health", {}).get("Status") != "healthy":
                raise CaptureError(f"Staging {name} must be running and healthy before capture")
        if self.compose.run("ps", "--status", "running", "-q", "migrate").strip():
            raise CaptureError("A database maintenance service is currently running")

        image_id = services["web"]["Image"]
        reference = self.config["image_reference"]
        if not isinstance(reference, str) or not reference or not re.fullmatch(r"[0-9a-f]{40}", self.config["git_sha"]):
            raise CaptureError("Capture needs a release commit and independently recoverable image reference")
        image = json.loads(self.compose.docker("image", "inspect", image_id))[0]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) or image["Os"] != "linux":
            raise CaptureError("Invalid running application image identity")
        if services["printer"]["Image"] != image_id:
            raise CaptureError("Web and printer must use the same application release")
        platform = f'{image["Os"]}/{image["Architecture"]}'
        if image.get("Variant"):
            platform += f'/{image["Variant"]}'
        capture_image = {"digest": image_id, "digest_type": "docker-image-id", "reference": reference, "platform": platform}

        stage = self.incomplete / cut_id
        stage.mkdir(mode=0o700)
        stage_info = stage.stat()
        ownership = {"cut_id": cut_id, "device": stage_info.st_dev, "inode": stage_info.st_ino}
        save_json(self.attempts / f"{cut_id}.json", ownership)
        fsync_directory(self.incomplete)

        started = monotonic()
        return {
            "cut_id": cut_id, "boot_id": boot_id(), "started_at": utc_now(),
            "cancel_monotonic": started + CANCEL_SECONDS, "end_monotonic": started + WINDOW_SECONDS,
            "runner": process_info(os.getpid()), "status": "Capturing",
            "project": self.compose.project, "image": capture_image,
            "services": {name: {"id": data["Id"], "running": data["State"]["Running"]} for name, data in services.items()},
        }

    def owned_stage(self, cut_id):
        if not re.fullmatch(r"[0-9a-f]{32}", cut_id):
            raise CaptureError("Invalid capture attempt identifier")
        stage = self.incomplete / cut_id
        record = read_json(self.attempts / f"{cut_id}.json")
        info = stage.lstat()
        if (
            record != {"cut_id": cut_id, "device": info.st_dev, "inode": info.st_ino}
            or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
        ):
            raise CaptureError("Capture stage ownership could not be verified")
        return stage

    def clean_incomplete(self):
        for path in self.incomplete.iterdir():
            try:
                stage = self.owned_stage(path.name)
            except (CaptureError, OSError, ValueError):
                print("Unverified raw capture retained for operations review", file=sys.stderr)
                continue
            if datetime.now(timezone.utc).timestamp() - stage.stat().st_mtime >= RAW_MAX_AGE_SECONDS:
                print(f"Raw capture exceeded the retention limit: {path.name}", file=sys.stderr)
            shutil.rmtree(stage)
            (self.attempts / f"{path.name}.json").unlink()
        fsync_directory(self.incomplete)
        fsync_directory(self.attempts)

    def record_cancellation(self, marker):
        if marker["status"] == "Cancelled":
            return
        # Reserve was allocated before admission closed so a full bundle volume can be recovered.
        self.reserve.unlink(missing_ok=True)
        marker["status"] = "Cancelled"
        marker["cancelled_at"] = utc_now()
        save_json(self.marker, marker)

    def wait_healthy(self, container, deadline):
        while monotonic() < deadline:
            state = self.compose.inspect(container)["State"]
            if state["Running"] and state.get("Health", {}).get("Status") == "healthy":
                return
            sleep(0.25)
        raise CaptureError("Staging did not reach healthy service before the recovery deadline")

    def close_capture_sessions(self, cut_id, deadline):
        if not re.fullmatch(r"[0-9a-f]{32}", cut_id):
            raise CaptureError("Invalid capture session identifier")
        predicate = (
            f"application_name = 's1-13:{cut_id}' AND datname = current_database() AND pid <> pg_backend_pid()"
        )
        statement = (
            f"SELECT pg_terminate_backend(pid, 5000) FROM pg_stat_activity WHERE {predicate};\n"
            f"SELECT count(*) FROM pg_stat_activity WHERE {predicate};\n"
        )
        results = self.compose.postgres(
            'exec psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f -',
            input=statement.encode(), timeout=max(1, min(12, deadline - monotonic())),
        ).splitlines()
        if not results or results[-1] != b"0":
            raise CaptureError("A tagged PostgreSQL capture session is still active")

    def reopen(self, marker):
        if marker["project"] != self.compose.project:
            raise CaptureError("Maintenance marker belongs to a different Compose project")
        same_boot = marker["boot_id"] == boot_id()
        official_end = marker["end_monotonic"]
        deadline = official_end if same_boot and monotonic() < official_end else monotonic() + WINDOW_SECONDS - CANCEL_SECONDS
        for name, state in marker["services"].items():
            if self.compose.container(name)["Id"] != state["id"]:
                raise CaptureError("Maintenance cannot reopen a different container")
        web = marker["services"]["web"]["id"]
        if marker["status"] == "Cancelled" and self.compose.inspect(web)["State"]["Running"]:
            # The Engine may still be delivering a stop after its original CLI has exited.
            self.compose.docker("stop", "--time", "5", web, timeout=max(1, min(20, deadline - monotonic())))
            if self.compose.inspect(web)["State"]["Running"]:
                raise CaptureError("The original writer has not settled in the stopped state")
        for name in ("db", "printer", "web"):
            status = marker["services"][name]
            if not status["running"]:
                continue
            container = self.compose.inspect(status["id"])
            if not container["State"]["Running"]:
                self.compose.docker("start", status["id"], timeout=max(1, min(30, deadline - monotonic())))
            self.wait_healthy(status["id"], deadline)
            if name == "db":
                self.close_capture_sessions(marker["cut_id"], deadline)
        return not same_boot or monotonic() <= official_end

    def clear_marker(self):
        self.marker.unlink(missing_ok=True)
        self.reserve.unlink(missing_ok=True)
        fsync_directory(self.state)

    def watchdog(self):
        with locked(self.state / "reopen.lock"):
            marker = self.read_marker()
            if marker is None:
                return
            if marker["boot_id"] == boot_id() and marker["status"] == "Capturing" and (
                monotonic() < marker["cancel_monotonic"] and same_process(marker["runner"])
            ):
                return
            self.record_cancellation(marker)
            stop_worker(marker)
            timely = self.reopen(marker)
            self.clear_marker()
            if not timely:
                raise CaptureError("Writing returned after the Staging recovery window expired")

    def worker(self, cut_id):
        # The parent creates a durable marker before this worker may stop web.
        deadline = monotonic() + 10
        while monotonic() < deadline:
            marker = self.read_marker()
            if marker and marker["cut_id"] == cut_id and marker["worker"]["pid"] == os.getpid():
                break
            sleep(0.05)
        else:
            raise CaptureError("Capture worker did not receive a durable authorization")
        if marker["status"] != "Capturing" or not same_process(marker["runner"]):
            raise CaptureError("Capture owner is no longer active")

        stage = self.owned_stage(cut_id)
        container = marker["services"]["web"]["id"]
        self.compose.run("stop", "--timeout", str(DRAIN_SECONDS), "web", timeout=DRAIN_SECONDS + 2)
        state = self.compose.inspect(container)["State"]
        if state["Running"] or state["ExitCode"] != 0:
            raise CaptureError("Gunicorn did not drain successfully")
        cut_at = utc_now()
        application_name = f"s1-13:{cut_id}"
        evidence = json.loads(self.compose.postgres(
            'exec psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f -',
            input=DATABASE_EVIDENCE_SQL.encode(), application_name=application_name,
        ))
        if evidence["postgres"]["server_version_num"] // 10000 != 18:
            raise CaptureError("Capture requires the approved PostgreSQL 18 release")
        dump = stage / "postgres.dump"
        with dump.open("xb") as writer:
            self.compose.postgres(
                'exec pg_dump -Fc --no-owner --no-privileges -U "$POSTGRES_USER" "$POSTGRES_DB"',
                stdout=writer, timeout=CANCEL_SECONDS, application_name=application_name,
            )
            writer.flush()
            os.fsync(writer.fileno())
        with dump.open("rb") as reader:
            if reader.read(5) != b"PGDMP":
                raise CaptureError("PostgreSQL custom archive was not created")
        files = {"postgres.dump": file_sha(dump)}
        attachments = stage / "attachments"
        attachments.mkdir(mode=0o700)
        for attachment in evidence["attachments"]:
            key = attachment["storage_key"]
            copy_attachment(container, attachment, attachments / key)
            files[f"attachments/{key}"] = attachment["sha256"]
        fsync_directory(attachments)
        if evidence["counts"]["test_transactions_attachment"] != len(files) - 1:
            raise CaptureError("Committed attachment count differs from captured files")
        save_json(stage / "manifest.json", evidence | {
            "version": 1, "cut_id": cut_id, "cut_at": cut_at, "git_sha": self.config["git_sha"],
            "image": marker["image"], "files": files, "capture_state": "Captured",
        })

    def capture(self):
        with locked(self.state / "capture.lock", wait=False):
            self.watchdog()
            if self.read_marker() is not None:
                raise CaptureError("Another maintenance window has not been reconciled")
            self.clean_incomplete()
            cut_id = uuid4().hex
            marker = self.prepare(cut_id)
            with locked(self.state / "reopen.lock"):
                self.reserve.unlink(missing_ok=True)
                with self.reserve.open("xb") as stream:
                    stream.write(os.urandom(64 * 1024))
                    stream.flush()
                    os.fsync(stream.fileno())
                runner = subprocess.Popen([
                    sys.executable, str(Path(__file__).resolve()), "--config", str(self.config_path), "worker", cut_id,
                ], start_new_session=True)
                try:
                    marker["worker"] = process_info(runner.pid)
                    save_json(self.marker, marker)
                except BaseException:
                    runner.kill()
                    runner.wait()
                    self.reserve.unlink(missing_ok=True)
                    raise
            succeeded = False
            try:
                succeeded = runner.wait(timeout=max(1, marker["cancel_monotonic"] - monotonic())) == 0
            finally:
                with locked(self.state / "reopen.lock"):
                    active = self.read_marker()
                    if active and active["cut_id"] == cut_id:
                        valid = (succeeded and active["status"] == "Capturing" and monotonic() < active["cancel_monotonic"])
                        if not valid:
                            self.record_cancellation(active)
                        stop_worker(active)
                        timely = self.reopen(active)
                        if valid and timely:
                            stage = self.owned_stage(cut_id)
                            manifest_path = stage / "manifest.json"
                            manifest = read_json(manifest_path)
                            manifest["capture_state"] = "Finalized"
                            manifest["writing_restored_at"] = utc_now()
                            save_json(manifest_path, manifest)
                            target = self.bundles / cut_id
                            stage.rename(target)
                            fsync_directory(self.bundles)
                            fsync_directory(self.incomplete)
                            self.clear_marker()
                        else:
                            self.clear_marker()
                            if not timely:
                                raise CaptureError("Writing returned after the Staging recovery window expired")
            target = self.bundles / cut_id
            if not target.is_dir():
                raise CaptureError("Capture cancelled or failed; no bundle was finalized")
            return target


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("capture")
    commands.add_parser("watchdog")
    worker = commands.add_parser("worker", help=argparse.SUPPRESS)
    worker.add_argument("cut_id")
    args = parser.parse_args()
    try:
        capture = Capture(args.config)
        if args.command == "capture":
            print(capture.capture())
        elif args.command == "watchdog":
            capture.watchdog()
        else:
            capture.worker(args.cut_id)
    except (CaptureError, OSError, KeyError, ValueError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print(f"S1-13: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
