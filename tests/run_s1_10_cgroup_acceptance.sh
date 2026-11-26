#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

normal_bytes=1073741824
limited_bytes=100663296
driver=$(docker info --format '{{.CgroupDriver}}/{{.CgroupVersion}}')
case "$driver" in
  systemd/2|cgroupfs/2) printf 'Docker cgroup driver: %s\n' "$driver" ;;
  *) printf 'A Docker cgroup v2 driver is required, but got %s\n' "$driver" >&2; exit 1 ;;
esac

umask 077
scratch=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/s110-cgroup.XXXXXX")
project="s110-cgroup-$(basename "$scratch" | sed 's/.*\.//' | tr '[:upper:]' '[:lower:]')"
export ERP_PRINT_MEMORY_LIMIT=1g

compose() {
  docker compose --project-name "$project" --env-file "$scratch/test.env" \
    -f compose.yaml -f "$scratch/cgroup.yaml" "$@"
}

cleanup() {
  local status=$?
  trap - EXIT
  if ((status)); then
    compose logs --no-color --tail 45 db web printer >&2 || true
  fi
  compose down --volumes --remove-orphans || true
  docker image rm "${project}-erp-app" >/dev/null 2>&1 || true
  rm -rf -- "$scratch"
  exit "$status"
}

python3 - "$scratch/test.env" <<'PY'
import socket
import sys
from pathlib import Path
from secrets import token_hex

with socket.socket() as socket:
    socket.bind(("127.0.0.1", 0))
    port = socket.getsockname()[1]

settings = {
    "ERP_DB_NAME": "erp_print_test",
    "ERP_DB_USER": "erp_print_owner",
    "ERP_DB_PASSWORD": token_hex(24),
    "ERP_WEB_DB_USER": "erp_print_web",
    "ERP_WEB_DB_PASSWORD": token_hex(24),
    "ERP_DJANGO_SECRET_KEY": token_hex(32),
    "ERP_WEB_PORT": port,
    "QA_PRINT_TEST_PASSWORD": token_hex(24),
}
Path(sys.argv[1]).write_text("".join(f"{key}={value}\n" for key, value in settings.items()))
PY
cat > "$scratch/cgroup.yaml" <<'YAML'
services:
  printer:
    memswap_limit: ${ERP_PRINT_MEMORY_LIMIT:?Set the print acceptance memory limit}
YAML
cat > "$scratch/limited.yaml" <<'YAML'
services:
  printer:
    restart: 'no'
YAML
trap cleanup EXIT

set -a
source "$scratch/test.env"
set +a

if ! compose --progress plain build web > "$scratch/build.log" 2>&1; then
  tail -80 "$scratch/build.log" >&2
  exit 1
fi
docker image inspect "${project}-erp-app" --format 'Acceptance image {{.Id}}'
compose up -d --no-build --wait db
compose exec -T -e ERP_WEB_DB_USER -e ERP_WEB_DB_PASSWORD db \
  psql -v ON_ERROR_STOP=1 -U "$ERP_DB_USER" -d "$ERP_DB_NAME" <<'SQL'
\getenv runtime_role ERP_WEB_DB_USER
\getenv runtime_password ERP_WEB_DB_PASSWORD
SELECT format(
    'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'runtime_role', :'runtime_password'
) \gexec
SQL
compose run --rm --no-deps -T migrate
for grants in ops/postgres/s1_05_runtime_grants.sql ops/postgres/s1_08_inventory_runtime_grants.sql; do
  compose exec -T db psql -v ON_ERROR_STOP=1 -U "$ERP_DB_USER" -d "$ERP_DB_NAME" \
    -v runtime_role="$ERP_WEB_DB_USER" < "$grants"
done
compose run --rm --no-deps -T migrate python manage.py loaddata s1_03_demo
compose run --rm --no-deps -T -e QA_PRINT_TEST_PASSWORD migrate \
  python manage.py shell --verbosity 0 -c 'from tests.s1_10_cgroup_probe import seed; seed()' > "$scratch/print-path"
compose up -d --no-build --wait printer web

printer_cgroup() {
  local pid relative
  pid=$(docker inspect --format '{{.State.Pid}}' "$1")
  [[ "$pid" =~ ^[1-9][0-9]*$ ]]
  relative=$(awk -F: '$1 == "0" && $2 == "" { print $3; exit }' "/proc/$pid/cgroup")
  [[ "$relative" == /* && "$relative" != / ]]
  printf '/sys/fs/cgroup%s\n' "$relative"
}

assert_cap() {
  local cgroup=$1 wanted=$2 memory swap
  memory=$(cat "$cgroup/memory.max")
  swap=$(cat "$cgroup/memory.swap.max")
  printf 'Effective printer cgroup limit: memory.max=%s memory.swap.max=%s\n' "$memory" "$swap"
  [[ "$memory" == "$wanted" && "$swap" == 0 ]]
}

counter() {
  awk -v name="$2" '$1 == name { value = $2 } END { if (value == "") exit 1; print value }' "$1/memory.events"
}

print_request() {
  python3 -m tests.s1_10_cgroup_probe --port "$ERP_WEB_PORT" --path-file "$scratch/print-path" "$@"
}

web_health() {
  curl --fail --silent --show-error --max-time 8 --output /dev/null \
    --write-out 'Web /healthz/ returned HTTP %{http_code}\n' "http://127.0.0.1:$ERP_WEB_PORT/healthz/"
}

web_database() {
  compose exec -T web python manage.py shell --verbosity 0 -c 'from tests.s1_10_cgroup_probe import database; database()'
}

lock_inode() {
  compose exec -T printer stat -c '%i' /var/lib/erp/print/render.lock
}

normal_container=$(compose ps -q printer)
normal_cgroup=$(printer_cgroup "$normal_container")
assert_cap "$normal_cgroup" "$normal_bytes"
if ! print_request --expect 200 --pdf "$scratch/normal.pdf"; then
  # Run the renderer directly with synthetic HTML to expose its diagnostic stderr.
  compose exec -T printer python -m apps.printing.renderer >/dev/null <<'HTML' || true
<!doctype html>
<html lang="ar" dir="rtl">
<head><meta charset="utf-8"><link rel="stylesheet" href="https://assets.erp.invalid/print.css"></head>
<body>الطباعة العربية PO-17</body>
</html>
HTML
  exit 1
fi
peak=$(cat "$normal_cgroup/memory.peak")
printf 'Normal print container measured memory.peak=%s bytes\n' "$peak"
test "$peak" -gt "$limited_bytes"
inode=$(lock_inode)
web=$(compose ps -q web)
database=$(compose ps -q db)

compose stop printer
compose rm -f printer
ERP_PRINT_MEMORY_LIMIT=96m compose -f "$scratch/limited.yaml" up -d --no-build --no-deps --wait printer
limited_container=$(compose ps -q printer)
limited_cgroup=$(printer_cgroup "$limited_container")
assert_cap "$limited_cgroup" "$limited_bytes"
before_oom=$(counter "$limited_cgroup" oom_kill)

print_request --expect 503 &
printing=$!
web_health
wait "$printing"
web_health
web_database

if [[ -r "$limited_cgroup/memory.events" ]]; then
  after_oom=$(counter "$limited_cgroup" oom_kill)
else
  after_oom=$before_oom
fi
oom_killed=$(docker inspect --format '{{.State.OOMKilled}}' "$limited_container")
printf 'Printer OOM evidence: oom_kill before=%s after=%s; Docker State.OOMKilled=%s\n' \
  "$before_oom" "$after_oom" "$oom_killed"
[[ "$after_oom" -gt "$before_oom" || "$oom_killed" == true ]]

compose stop printer
compose rm -f printer
compose up -d --no-build --no-deps --wait printer
recovered_container=$(compose ps -q printer)
assert_cap "$(printer_cgroup "$recovered_container")" "$normal_bytes"
[[ "$(compose ps -q web)" == "$web" && "$(compose ps -q db)" == "$database" ]]
[[ "$(lock_inode)" == "$inode" ]]
print_request --expect 200 --pdf "$scratch/recovered.pdf"
web_health
web_database
