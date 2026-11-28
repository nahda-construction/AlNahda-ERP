#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
repo=$(pwd -P)
command -v restic >/dev/null
command -v pdftotext >/dev/null
umask 077
scratch=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/s113-restore.XXXXXX")
suffix=$(basename "$scratch" | sed 's/.*\.//' | tr '[:upper:]' '[:lower:]')
origin="s113-origin-$suffix"
target="s113-target-$suffix"
mkdir -m 700 "$scratch/backup-state" "$scratch/restore-state" "$scratch/offsite"

compose() {
  local side=$1 project
  shift
  case "$side" in
    origin) project=$origin ;;
    target) project=$target ;;
    *) return 2 ;;
  esac
  docker compose --project-name "$project" --env-file "$scratch/$side.env" -f "$repo/compose.yaml" "$@"
}

cleanup() {
  local status=$? cleanup_status=0
  trap - EXIT
  if ((status)); then
    compose origin logs --no-color --tail 20 web printer >&2 || true
    compose target logs --no-color --tail 20 web printer >&2 || true
  fi
  compose target down --volumes --remove-orphans || cleanup_status=$?
  compose origin down --volumes --remove-orphans || cleanup_status=$?
  docker image rm "$target-erp-app" "$origin-erp-app" >/dev/null 2>&1 || true
  rm -rf -- "$scratch"
  if ((status == 0 && cleanup_status)); then status=$cleanup_status; fi
  exit "$status"
}

python3 - "$scratch" <<'PY'
import socket
import sys
from pathlib import Path
from secrets import token_hex

root = Path(sys.argv[1])
with socket.socket() as first, socket.socket() as second:
    first.bind(("127.0.0.1", 0))
    second.bind(("127.0.0.1", 0))
    for side, probe in (("origin", first), ("target", second)):
        values = {
            "ERP_DB_NAME": f"erp_{side}", "ERP_DB_USER": f"{side}_migration",
            "ERP_DB_PASSWORD": token_hex(24), "ERP_WEB_DB_USER": f"{side}_runtime",
            "ERP_WEB_DB_PASSWORD": token_hex(24), "ERP_DJANGO_SECRET_KEY": token_hex(32),
            "ERP_WEB_PORT": probe.getsockname()[1],
        }
        (root / f"{side}.env").write_text("".join(f"{key}={value}\n" for key, value in values.items()))

users = {"S1_12_PASSWORD_A": token_hex(24), "S1_12_PASSWORD_B": token_hex(24)}
(root / "staff.env").write_text("".join(f"{key}={value}\n" for key, value in users.items()))
for secret in ("source-password", "offsite/destination-password", "wrong-password"):
    (root / secret).write_text(token_hex(24) + "\n")
PY

for project in "$origin" "$target"; do
  for resource in container network volume; do
    if [[ "$resource" == container ]]; then
      existing=$(docker container ls -aq --filter "label=com.docker.compose.project=$project")
    else
      existing=$(docker "$resource" ls -q --filter "label=com.docker.compose.project=$project")
    fi
    [[ -z "$existing" ]] || { echo 'Unexpected Compose resource collision' >&2; exit 1; }
  done
  [[ -z "$(docker image ls -q --filter "reference=$project-erp-app")" ]]
done
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

set -a
source "$scratch/staff.env"
set +a
unset RESTIC_PASSWORD RESTIC_PASSWORD_COMMAND RESTIC_REPOSITORY_FILE
export RESTIC_REPOSITORY="$scratch/source-restic"
export RESTIC_PASSWORD_FILE="$scratch/source-password"
export RESTIC_CACHE_DIR="$scratch/restic-cache"
export S1_13_DEST_REPOSITORY="$scratch/offsite/restic"
export S1_13_DEST_PASSWORD_FILE="$scratch/offsite/destination-password"

provision_database() (
  set -a
  source "$scratch/$1.env"
  set +a
  compose "$1" up -d --no-build --wait db
  compose "$1" exec -T -e ERP_WEB_DB_USER -e ERP_WEB_DB_PASSWORD db \
    psql -v ON_ERROR_STOP=1 -U "$ERP_DB_USER" -d "$ERP_DB_NAME" <<'SQL'
\getenv runtime_role ERP_WEB_DB_USER
\getenv runtime_password ERP_WEB_DB_PASSWORD
SELECT format(
    'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'runtime_role', :'runtime_password'
) \gexec
SQL
)

if ! compose origin --progress plain build web > "$scratch/build.log" 2>&1; then
  tail -80 "$scratch/build.log" >&2
  exit 1
fi
image=$(docker image inspect "$origin-erp-app" --format '{{.Id}}')
docker image save -o "$scratch/offsite/app.tar" "$origin-erp-app"
printf 'Synthetic source image ID: %s\n' "$image"
provision_database origin
compose origin run --rm --no-deps -T migrate
(
  source "$scratch/origin.env"
  for grants in ops/postgres/s1_05_runtime_grants.sql ops/postgres/s1_08_inventory_runtime_grants.sql; do
    compose origin exec -T db psql -v ON_ERROR_STOP=1 -U "$ERP_DB_USER" -d "$ERP_DB_NAME" \
      -v runtime_role="$ERP_WEB_DB_USER" < "$grants"
  done
)
compose origin run --rm --no-deps -T migrate python manage.py loaddata s1_03_demo
compose origin run --rm --no-deps -T -e S1_12_PASSWORD_A -e S1_12_PASSWORD_B migrate \
  python manage.py shell --verbosity 0 -c 'from tests.s1_12_acceptance import seed; seed()'
compose origin up -d --no-build --wait web printer
compose origin exec -T web python manage.py shell --verbosity 0 \
  -c 'from tests.test_s1_13_backup import seed_document; seed_document()' > "$scratch/fixture.json"
compose origin exec -T web python -c '
import os
from pathlib import Path
from uuid import uuid4
path = Path(os.environ["ERP_PRIVATE_ROOT"]) / (uuid4().hex + ".pdf")
path.write_bytes(b"%PDF-1.4\n% Private synthetic orphan\n")
path.chmod(0o600)
'

python3 - "$scratch" "$repo" "$origin" "$target" "$image" "$(git rev-parse HEAD)" <<'PY'
import json
import sys
from pathlib import Path

workspace, checkout, origin, target, image, commit = sys.argv[1:]
root = Path(workspace)
capture = {
    "state": str(root / "backup-state"), "compose_dir": checkout,
    "compose_env": str(root / "origin.env"), "compose_project": origin,
    "compose_files": [f"{checkout}/compose.yaml"], "git_sha": commit,
    "image_reference": image, "minimum_free_bytes": 1024 * 1024,
}
restore = {
    "project": target, "source_project": origin, "env_file": str(root / "target.env"),
    "state_dir": str(root / "restore-state"), "repository": str(root / "offsite/restic"),
    "password_file": str(root / "offsite/destination-password"), "app_image": image,
}
for name, config in (
    ("capture", capture), ("insufficient", capture | {"minimum_free_bytes": 2 ** 60}),
    ("restore", restore), ("wrong-key", restore | {"password_file": str(root / "wrong-password")}),
):
    (root / f"{name}.json").write_text(json.dumps(config))
PY

source_port=$(sed -n 's/^ERP_WEB_PORT=//p' "$scratch/origin.env")
if python3 ops/backup/capture.py --config "$scratch/insufficient.json" capture; then
  echo 'Capture accepted a request with insufficient configured capacity' >&2
  exit 1
fi
curl --silent --show-error --fail --max-time 8 "http://127.0.0.1:$source_port/healthz/"

storage_key=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["storage_key"])' "$scratch/fixture.json")
compose origin exec -T web python -c '
import os, sys
from pathlib import Path
path = Path(os.environ["ERP_PRIVATE_ROOT"]) / sys.argv[1]
path.rename(path.with_suffix(".missing-fixture"))
' "$storage_key"
if python3 ops/backup/capture.py --config "$scratch/capture.json" capture; then
  echo 'Capture unexpectedly accepted a missing committed attachment' >&2
  exit 1
fi
curl --silent --show-error --fail --max-time 8 "http://127.0.0.1:$source_port/healthz/"
compose origin exec -T web python -c '
import os, sys
from pathlib import Path
path = Path(os.environ["ERP_PRIVATE_ROOT"]) / sys.argv[1]
path.with_suffix(".missing-fixture").rename(path)
' "$storage_key"

bundle=$(python3 ops/backup/capture.py --config "$scratch/capture.json" capture)
python3 - "$bundle" "$scratch/fixture.json" "$image" <<'PY'
import json
import sys
from pathlib import Path

bundle = Path(sys.argv[1])
fixture = json.loads(Path(sys.argv[2]).read_bytes())
manifest = json.loads((bundle / "manifest.json").read_bytes())
recorded_file = f'attachments/{fixture["storage_key"]}'
assert manifest["capture_state"] == "Finalized" and manifest["image"]["digest"] == sys.argv[3]
assert set(manifest["files"]) == {"postgres.dump", recorded_file}
assert manifest["files"][recorded_file] == fixture["attachment_sha256"]
with (bundle / "postgres.dump").open("rb") as dump:
    assert dump.read(5) == b"PGDMP"
print("Finalized PostgreSQL archive and committed attachment; orphan excluded")
PY
restic init
restic --repo "$S1_13_DEST_REPOSITORY" --password-file "$S1_13_DEST_PASSWORD_FILE" init
python3 ops/backup/staging.py --config "$scratch/capture.json" transfer
test ! -e "$bundle"
restic check --read-data
restic --repo "$S1_13_DEST_REPOSITORY" --password-file "$S1_13_DEST_PASSWORD_FILE" check --read-data
python3 ops/backup/transport.py --state "$scratch/backup-state/transport" status > "$scratch/offsite/verified.json"
verified=$(python3 - "$scratch/offsite/verified.json" <<'PY'
import json
import sys

with open(sys.argv[1]) as stream:
    status = json.load(stream)
[job] = status["jobs"]
assert job["state"] == "Verified" and not status["rpo_alert"]
print(job["destination_snapshot_id"], job["cut_id"], job["manifest_sha256"])
PY
)
read -r snapshot cut fingerprint <<< "$verified"

loss_at=$(date -u +%FT%TZ)
restore_started=$SECONDS
compose origin down --volumes --remove-orphans
[[ -z "$(docker container ls -aq --filter "label=com.docker.compose.project=$origin")" ]]
[[ -z "$(docker volume ls -q --filter "label=com.docker.compose.project=$origin")" ]]
mv "$RESTIC_REPOSITORY" "$scratch/unavailable-source-restic"
mv "$RESTIC_PASSWORD_FILE" "$scratch/unavailable-source-password"
docker image rm "$origin-erp-app"
docker image load -i "$scratch/offsite/app.tar"
[[ "$(docker image inspect "$origin-erp-app" --format '{{.Id}}')" == "$image" ]]
provision_database target

restore_job() {
  python3 ops/backup/restore.py --config "$1" --snapshot "$snapshot" --cut "$cut" \
    --manifest-sha256 "$fingerprint" --verified-job "$scratch/offsite/verified.json" --synthetic-verify
}
if restore_job "$scratch/wrong-key.json"; then
  echo 'Restore accepted the wrong offsite password' >&2
  exit 1
fi
restore_job "$scratch/restore.json" | tee "$scratch/restore-result.json"
target_port=$(sed -n 's/^ERP_WEB_PORT=//p' "$scratch/target.env")
python3 - "$target_port" "$scratch/fixture.json" "$scratch/restored.pdf" <<'PY'
import sys

from tests.test_s1_13_backup import verify_restored_web

verify_restored_web(int(sys.argv[1]), sys.argv[2], sys.argv[3])
PY
python3 - "$scratch/restore-result.json" "$loss_at" "$((SECONDS - restore_started))" <<'PY'
import datetime
import json
import sys

with open(sys.argv[1]) as report:
    restored = json.load(report)
loss = datetime.datetime.fromisoformat(sys.argv[2])
cut = datetime.datetime.fromisoformat(restored["cut_at"])
rpo = (loss - cut).total_seconds()
assert 0 <= rpo <= 24 * 60 * 60
print(json.dumps({"synthetic_rpo_seconds": rpo, "synthetic_rto_seconds": int(sys.argv[3])}))
PY
