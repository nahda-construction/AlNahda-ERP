"""Verify an external Restic snapshot and restore it into a clean Staging project."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
from tempfile import TemporaryDirectory, TemporaryFile

if __package__:
    from .capture import DATABASE_EVIDENCE_SQL
    from .transport import BackupError, Queue, Restic
else:
    from capture import DATABASE_EVIDENCE_SQL
    from transport import BackupError, Queue, Restic


REPO = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO / "compose.yaml"
PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
STORAGE_KEY = re.compile(r"[0-9a-f]{32}\.pdf\Z")
EVIDENCE_KEYS = ("postgres", "migrations", "counts", "control_totals", "attachments")
APP_VOLUMES = ("pgdata", "privatefiles", "printlock")


OWNER_SQL = """
SELECT json_build_object(
    'migration_role', current_user,
    'database_owner_matches', (SELECT datdba = current_user::regrole FROM pg_database WHERE datname = current_database()),
    'relations_owner_matches', NOT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relowner <> current_user::regrole
    ),
    'functions_owner_matches', NOT EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.proowner <> current_user::regrole
    )
);
"""


RUNTIME_PROBE_SQL = """
BEGIN;
SET LOCAL lock_timeout = '5s';
DO $probe$
DECLARE
    source_project uuid;
    source_actor bigint;
    probe_uuid uuid := gen_random_uuid();
    probe_audit_id bigint;
    probe_receipt_id bigint;
    protected_relation text;
    operation text;
    failed_constraint text;
BEGIN
    SELECT project_id, created_by_id INTO STRICT source_project, source_actor
    FROM public.test_transactions_testtransaction ORDER BY id LIMIT 1;

    INSERT INTO public.test_transactions_testtransaction
        (id, project_id, document_no, title, status, created_by_id, created_at)
    VALUES (probe_uuid, source_project, 'TST-' || probe_uuid, 'Synthetic restore probe',
            'DRAFT', source_actor, now());

    -- Explicit unused negative IDs keep sequence positions unchanged after rollback.
    SELECT least(coalesce(min(id), 0), 0) - 1 INTO probe_audit_id
    FROM public.test_transactions_auditevent;
    INSERT INTO public.test_transactions_auditevent
        (id, project_id, actor_id, event_type, occurred_at, correlation_id, test_transaction_id, next_state)
    VALUES (probe_audit_id, source_project, source_actor, 'CREATED', now(), probe_uuid, probe_uuid, 'DRAFT');

    SELECT least(coalesce(min(id), 0), 0) - 1 INTO probe_receipt_id
    FROM public.test_transactions_idempotencyrecord;
    INSERT INTO public.test_transactions_idempotencyrecord
        (id, actor_id, project_id, operation, key, payload_sha256, status, created_at)
    VALUES (probe_receipt_id, source_actor, source_project, 'CREATE_TEST_DRAFT', probe_uuid,
            repeat('0', 64), 'IN_PROGRESS', now());
    UPDATE public.test_transactions_idempotencyrecord
        SET status = 'COMPLETED', test_transaction_id = probe_uuid, response_status = 303,
            response_body = jsonb_build_object('transaction_id', probe_uuid)
        WHERE id = probe_receipt_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'Could not complete runtime receipt'; END IF;

    BEGIN
        UPDATE public.test_transactions_idempotencyrecord
            SET response_status = 201 WHERE id = probe_receipt_id;
        RAISE EXCEPTION 'Runtime role could rewrite the completed receipt';
    EXCEPTION WHEN check_violation THEN
        GET STACKED DIAGNOSTICS failed_constraint = CONSTRAINT_NAME;
        IF failed_constraint <> 'idem_completed_receipt_immutable' THEN RAISE; END IF;
    END;

    FOREACH protected_relation IN ARRAY ARRAY[
        'access_control_projectaccessevent', 'test_transactions_auditevent',
        'inventory_stockmovement', 'test_transactions_idempotencyrecord'
    ] LOOP
        EXECUTE format('SELECT count(*) FROM public.%I', protected_relation);
        FOREACH operation IN ARRAY ARRAY[
            'UPDATE public.%I SET id = id WHERE false',
            'DELETE FROM public.%I WHERE false', 'TRUNCATE TABLE public.%I'
        ] LOOP
            BEGIN
                EXECUTE format(operation, protected_relation);
                RAISE EXCEPTION 'Unexpected write privilege';
            EXCEPTION WHEN insufficient_privilege THEN
                NULL;
            END;
        END LOOP;
    END LOOP;

    BEGIN
        ALTER TABLE public.django_migrations ADD COLUMN s113_forbidden_probe integer;
        RAISE EXCEPTION 'Runtime role could change the migration schema';
    EXCEPTION WHEN insufficient_privilege THEN
        NULL;
    END;
END
$probe$;
SELECT current_user;
ROLLBACK;
"""


MOUNT_RESTORE = r"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tarfile

root = Path(os.environ["ERP_PRIVATE_ROOT"])
mode = root.lstat()
if not stat.S_ISDIR(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
    raise SystemExit("Unsafe target private file directory")
if any(root.iterdir()):
    raise SystemExit("Target private file directory is not empty")

files = []
with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as archive:
    for entry in archive:
        if not entry.isfile() or not re.fullmatch(r"[0-9a-f]{32}\.pdf", entry.name):
            raise SystemExit("Unsafe restored attachment")
        path = root / entry.name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as writer, archive.extractfile(entry) as reader:
            shutil.copyfileobj(reader, writer)
        with path.open("rb") as reader:
            files.append({
                "storage_key": entry.name,
                "size_bytes": path.stat().st_size,
                "sha256": hashlib.file_digest(reader, "sha256").hexdigest(),
            })
print(json.dumps(sorted(files, key=lambda row: row["storage_key"])))
"""


def run(command, action, *, content=None, stream=None):
    options = {"input": content} if content is not None else {"stdin": stream}
    try:
        return subprocess.run(command, capture_output=True, check=True, **options).stdout
    except (subprocess.CalledProcessError, OSError):
        # Docker, Restic and PostgreSQL diagnostics can contain deployment secrets.
        raise BackupError(f"{action} failed") from None


def private_path(value, *, directory=False):
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or path.is_relative_to(REPO):
        raise BackupError("Restore files must be canonical paths outside the checkout")
    info = path.stat()
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BackupError("Restore configuration and staging paths must be private")
    return path


def verified_job(path, cut, snapshot, fingerprint):
    status = json.loads(private_path(path).read_bytes())
    matches = [
        job for job in status["jobs"] if (
            job["state"] == "Verified"
            and job["cut_id"] == cut
            and job.get("destination_snapshot_id") == snapshot
            and job["manifest_sha256"] == fingerprint
        )
    ]
    if len(matches) != 1:
        raise BackupError("No unique Verified external transfer matches the requested restore")
    return matches[0]


def verify_bundle(repository, snapshot, cut, fingerprint, directory, synthetic):
    matches = repository.snapshots(f"s1-13:{cut}")
    if not any(item["id"] == snapshot for item in matches):
        raise BackupError("External snapshot does not match the approved cut")
    repository.verify(snapshot, fingerprint, full=False)
    repository.run("check", *(("--read-data",) if synthetic else ()))
    repository.run("restore", snapshot, "--target", str(directory))
    manifests = list(directory.rglob("manifest.json"))
    if len(manifests) != 1:
        raise BackupError("Restored package must contain one manifest")
    bundle = manifests[0].parent
    manifest, _ = Queue.inspect_bundle(bundle, fingerprint)
    if manifest["cut_id"] != cut:
        raise BackupError("Restored manifest belongs to a different cut")

    expected = {"postgres.dump"}
    attachments = sorted(manifest["attachments"], key=lambda row: row["storage_key"])
    for entry in attachments:
        key = entry["storage_key"]
        relative = f"attachments/{key}"
        if (
            not STORAGE_KEY.fullmatch(key) or relative in expected
            or manifest["files"].get(relative) != entry["sha256"]
            or (bundle / relative).stat().st_size != entry["size_bytes"]
        ):
            raise BackupError("Restored attachment does not match the manifest inventory")
        expected.add(relative)
    if manifest["files"].keys() != expected:
        raise BackupError("Manifest contains unexpected file paths")
    actual = set()
    for path in bundle.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise BackupError("Unexpected filesystem object in restored package")
        if path.is_file():
            actual.add(path.relative_to(bundle).as_posix())
    if actual != expected | {"manifest.json"}:
        raise BackupError("Restored package contains unrecorded files")
    return bundle, manifest, attachments


class Destination:
    def __init__(self, config):
        self.project = config["project"]
        source = config["source_project"]
        if not PROJECT.fullmatch(self.project) or not PROJECT.fullmatch(source) or source == self.project:
            raise BackupError("Source and restore Compose projects must be distinct")
        self.docker = ["docker"]
        if config.get("docker_context"):
            self.docker.extend(("--context", config["docker_context"]))
        self.compose = [
            *self.docker, "compose", "--file", str(COMPOSE_FILE), "--project-name", self.project,
            "--env-file", str(private_path(config["env_file"])),
        ]
        config_output = self.call("Inspecting target Compose", "config", "--format", "json")
        plan = json.loads(config_output)
        services = plan["services"]
        for name in APP_VOLUMES:
            volume = plan["volumes"][name]
            if volume["name"] != f"{self.project}_{name}" or volume.get("external") or volume.get("driver_opts"):
                raise BackupError("Target Compose uses a shared or non-local volume")
        for name, mounts in ((name, service.get("volumes", [])) for name, service in services.items()):
            if any(mount["type"] != "volume" or mount["source"] not in APP_VOLUMES for mount in mounts):
                raise BackupError(f"Unsafe mount in target service {name}")
        if "ports" in services["db"] or not plan["networks"]["database"].get("internal"):
            raise BackupError("Target database must remain on its private network")
        if services["printer"].get("ports"):
            raise BackupError("Target print service cannot publish a host port")
        web_ports = services["web"].get("ports", [])
        if len(web_ports) != 1 or web_ports[0].get("host_ip") not in ("127.0.0.1", "::1"):
            raise BackupError("Restored Staging can publish its web service only on loopback")
        for network in plan["networks"].values():
            if network.get("external") or not network["name"].startswith(self.project + "_"):
                raise BackupError("Target Compose networks must be project-local")
        web_mounts = services["web"].get("volumes", [])
        if not any(mount["source"] == "privatefiles" and mount["target"] == "/var/lib/erp/private" for mount in web_mounts):
            raise BackupError("Target Compose does not mount its private attachment volume")

        origin_filter = f"label=com.docker.compose.project={source}"
        if (
            self.docker_call("Checking source containers", "ps", "-aq", "--filter", origin_filter).strip()
            or self.docker_call("Checking source volumes", "volume", "ls", "-q", "--filter", origin_filter).strip()
        ):
            raise BackupError("The restore Docker host must be independent from source storage")

        own_filter = f"label=com.docker.compose.project={self.project}"
        target_containers = self.docker_call("Checking target containers", "ps", "-aq", "--filter", own_filter).split()
        active_db = self.call("Checking target database", "ps", "--status", "running", "-q", "db").split()
        if len(active_db) != 1 or len(target_containers) != 1 or not active_db[0].startswith(target_containers[0]):
            raise BackupError("Prepare a fresh target database and leave target application services stopped")
        container, = json.loads(self.docker_call("Inspecting target database storage", "inspect", active_db[0].decode()))
        if len(container["Mounts"]) != 1 or not (
            container["Mounts"][0]["Type"] == "volume"
            and container["Mounts"][0]["Name"] == f"{self.project}_pgdata"
            and container["Mounts"][0]["Destination"] == "/var/lib/postgresql"
        ):
            raise BackupError("Target database storage is not isolated")
        present = set(self.docker_call("Checking local restore volumes", "volume", "ls", "-q").decode().split())
        for name in APP_VOLUMES:
            volume_name = f"{self.project}_{name}"
            if volume_name not in present:
                continue
            volume, = json.loads(self.docker_call("Inspecting local restore volume", "volume", "inspect", volume_name))
            labels = volume["Labels"] or {}
            if (
                volume["Driver"] != "local" or volume["Options"]
                or labels.get("com.docker.compose.project") != self.project
                or labels.get("com.docker.compose.volume") != name
            ):
                raise BackupError("Existing target volume does not match the isolated Compose project")
        database = services["db"]["environment"]
        web = services["web"]["environment"]
        self.owner = database["POSTGRES_USER"]
        self.runtime = web["POSTGRES_USER"]
        self.runtime_password = web["POSTGRES_PASSWORD"]
        if (
            not self.runtime or self.owner == self.runtime
            or database["POSTGRES_DB"] != web["POSTGRES_DB"]
            or any(character in self.runtime_password for character in "\n\r\x00")
        ):
            raise BackupError("Invalid or shared PostgreSQL credentials in the target configuration")
        self.image_tag = services["web"]["image"]
        if self.image_tag != f"{self.project}-erp-app" or services["printer"]["image"] != self.image_tag:
            raise BackupError("Target web and printer must share the project-local released image")

    def call(self, action, *args, **options):
        return run([*self.compose, *args], action, **options)

    def docker_call(self, action, *args):
        return run([*self.docker, *args], action)

    def install_image(self, reference, image):
        inspected, = json.loads(self.docker_call("Inspecting independently fetched image", "image", "inspect", reference))
        image_id = inspected["Id"]
        digests = {item.rsplit("@", 1)[-1] for item in inspected.get("RepoDigests", [])}
        digest_type = image["digest_type"]
        if digest_type in ("docker-image-id", "image_id", "config", "config_digest"):
            valid = image["digest"] == image_id
        elif digest_type in ("docker-repo-digest", "repo_digest", "registry", "registry_digest"):
            valid = image["digest"].rsplit("@", 1)[-1] in digests
        else:
            valid = False
        platform = f'{inspected["Os"]}/{inspected["Architecture"]}'
        if inspected.get("Variant"):
            platform += f'/{inspected["Variant"]}'
        if not valid or image["platform"] != platform or inspected["Os"] != "linux":
            raise BackupError("The independently fetched image does not match the backup digest and platform")
        self.docker_call("Tagging verified application image", "image", "tag", image_id, self.image_tag)

    def owner_sql(self, sql, *variables):
        return self.call(
            "Checking the migration role or applying restored grants", "exec", "-T", "db", "sh", "-ec",
            'export PGPASSWORD="$POSTGRES_PASSWORD"; exec psql -XqAtw -h 127.0.0.1 '
            '-U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"',
            "_", *variables, content=sql.encode(),
        )

    def runtime_sql(self, sql):
        return self.call(
            "Checking independent runtime login or exercising its grants", "exec", "-T", "db", "sh", "-ec",
            'IFS= read -r PGPASSWORD; export PGPASSWORD; exec psql -XqAtw -h 127.0.0.1 '
            '-U "$1" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1',
            "_", self.runtime, content=f"{self.runtime_password}\n{sql}".encode(),
        )

    def assert_empty(self, manifest):
        actual = self.owner_sql(
            """SELECT json_build_object(
                'user', current_user,
                'version', current_setting('server_version_num'),
                'empty', NOT EXISTS (SELECT 1 FROM pg_class WHERE relnamespace = 'public'::regnamespace),
                'safe_runtime', NOT pg_has_role(:'runtime_role', current_user, 'SET')
                     AND NOT has_schema_privilege(:'runtime_role', 'public', 'CREATE')
             );""",
            "-v", f"runtime_role={self.runtime}",
        )
        measured = json.loads(actual)
        if (
            measured["user"] != self.owner or not measured["empty"] or not measured["safe_runtime"]
            or str(manifest["postgres"]["server_version_num"]) != measured["version"]
            or not measured["version"].startswith("18")
        ):
            raise BackupError("Target database is not a clean PostgreSQL 18 instance of the required version")
        if self.runtime_sql("SELECT current_user;").decode().strip() != self.runtime:
            raise BackupError("Target runtime credentials do not reach the independent login")

    def restore_database(self, dump):
        with dump.open("rb") as stream:
            self.call(
                "Restoring PostgreSQL in one transaction", "exec", "-T", "db", "sh", "-ec",
                'export PGPASSWORD="$POSTGRES_PASSWORD"; exec pg_restore -h 127.0.0.1 '
                '--username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --single-transaction '
                '--exit-on-error --no-owner --no-privileges',
                stream=stream,
            )
        for script in ("s1_05_runtime_grants.sql", "s1_08_inventory_runtime_grants.sql"):
            content = (REPO / "ops" / "postgres" / script).read_text()
            self.owner_sql(content, "-v", f"runtime_role={self.runtime}")

    def restore_files(self, directory, entries):
        with TemporaryFile() as stream:
            with tarfile.open(fileobj=stream, mode="w|") as archive:
                for entry in entries:
                    key = entry["storage_key"]
                    info = tarfile.TarInfo(key)
                    info.size = entry["size_bytes"]
                    info.mode = 0o600
                    with (directory / "attachments" / key).open("rb") as source:
                        archive.addfile(info, source)
            stream.seek(0)
            actual = self.call(
                "Restoring attachments into the project-local private volume", "run", "--rm", "-T",
                "--no-deps", "--pull", "never", "web", "python", "-c", MOUNT_RESTORE, stream=stream,
            )
        if json.loads(actual) != entries:
            raise BackupError("Restored private volume contents do not match manifest inventory")

    def verify_database(self, manifest):
        owner = json.loads(self.owner_sql(OWNER_SQL))
        if owner["migration_role"] != self.owner or not all(
            owner[key] for key in ("database_owner_matches", "relations_owner_matches", "functions_owner_matches")
        ):
            raise BackupError("Target application objects are not owned by the migration role")
        expected = {field: manifest[field] for field in EVIDENCE_KEYS}
        evidence_sql = DATABASE_EVIDENCE_SQL.decode() if isinstance(DATABASE_EVIDENCE_SQL, bytes) else DATABASE_EVIDENCE_SQL
        if json.loads(self.owner_sql(evidence_sql)) != expected:
            raise BackupError("Restored PostgreSQL evidence does not match the manifest")
        if self.runtime_sql(RUNTIME_PROBE_SQL).decode().strip() != self.runtime:
            raise BackupError("Independent runtime grant checks failed")
        if json.loads(self.owner_sql(evidence_sql)) != expected:
            raise BackupError("Runtime grant probes changed the restored data")
        return owner | {"runtime_role": self.runtime, "sql_grant_probes_rolled_back": True}


def restore(config, snapshot, cut, fingerprint, *, provenance, synthetic):
    if config.get("scope") != "isolated-staging":
        raise BackupError("The restore configuration must explicitly identify an isolated Staging target")
    if not FINGERPRINT.fullmatch(snapshot) or not FINGERPRINT.fullmatch(fingerprint):
        raise BackupError("Expected a full external snapshot ID and a manifest SHA-256")
    job = verified_job(provenance, cut, snapshot, fingerprint)
    state = private_path(config["state_dir"], directory=True)
    password = private_path(config["password_file"])
    repository = Restic(config["repository"], password)
    repository_id = job.get("destination_repository_id", "")
    if not FINGERPRINT.fullmatch(repository_id) or repository.identity() != repository_id:
        raise BackupError("Configured external repository does not match the verified transfer")
    started = datetime.now(timezone.utc)
    with TemporaryDirectory(prefix="s1-13-restore-", dir=state) as directory:
        bundle, manifest, attachments = verify_bundle(
            repository, snapshot, cut, fingerprint, Path(directory), synthetic,
        )
        target = Destination(config)
        target.assert_empty(manifest)
        target.install_image(config["app_image"], manifest["image"])
        target.restore_database(bundle / "postgres.dump")
        target.restore_files(bundle, attachments)
        roles = target.verify_database(manifest)
        target.call(
            "Starting matched restored web and printer", "up", "-d", "--no-build", "--pull", "never",
            "--wait", "--wait-timeout", "60", "web", "printer",
        )
    finished = datetime.now(timezone.utc)
    return {
        "cut_id": cut, "cut_at": manifest["cut_at"], "external_snapshot_id": snapshot,
        "backup_job_id": job["job_id"], "manifest_sha256": fingerprint, "git_sha": manifest["git_sha"],
        "image": manifest["image"], "postgres": manifest["postgres"],
        "migrations": manifest["migrations"], "counts": manifest["counts"],
        "control_totals": manifest["control_totals"], "attachment_count": len(attachments),
        "destination_repository_id": repository_id,
        "roles": roles, "started_at": started.isoformat(), "finished_at": finished.isoformat(),
        "application_acceptance": "pending", "restore_scope": "isolated-staging",
        "elapsed_seconds": (finished - started).total_seconds(), "synthetic": synthetic,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--cut", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--verified-job", required=True, type=Path)
    parser.add_argument("--synthetic-verify", action="store_true")
    args = parser.parse_args()
    config = json.loads(private_path(args.config).read_bytes())
    report = restore(
        config, args.snapshot, args.cut, args.manifest_sha256,
        provenance=args.verified_job, synthetic=args.synthetic_verify,
    )
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (BackupError, OSError, ValueError, TypeError, KeyError) as exc:
        safe = str(exc) if isinstance(exc, BackupError) else type(exc).__name__
        raise SystemExit(f"S1-13 restore: {safe}") from None
