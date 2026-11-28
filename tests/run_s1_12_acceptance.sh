#!/usr/bin/env bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

scratch=
started=$SECONDS
checksum_files=("$PWD/compose.yaml" "$PWD/compose.staging.yaml" "$PWD/tests/compose.s1-12.yaml")

fail() {
  printf 'S1-12: %s\n' "$*" >&2
  return 1
}

check_root() {
  local parent
  parent=$(realpath -e -- "${RUNNER_TEMP:-${TMPDIR:-/tmp}}") || return 1
  [[ "$scratch" == "$parent"/s112-* && -d "$scratch" && ! -L "$scratch" && -O "$scratch" ]] || return 1
  local name_token
  name_token=${scratch##*/s112-}
  [[ "$name_token" =~ ^[0-9a-f]{24}$ && "$scratch" == "$parent/s112-$name_token" ]] || return 1
  project="s112-$name_token"
}

compose() {
  local key
  local -a clean_env=(-u COMPOSE_PROJECT_NAME -u COMPOSE_FILE -u COMPOSE_PROFILES -u COMPOSE_ENV_FILES)
  while IFS='=' read -r key _; do
    [[ "$key" =~ ^[A-Z_][A-Z0-9_]*$ ]] || return 1
    clean_env+=(-u "$key")
  done < "$scratch/test.env"
  env "${clean_env[@]}" docker compose --project-name "$project" --env-file "$scratch/test.env" \
    -f "${checksum_files[0]}" -f "${checksum_files[1]}" -f "${checksum_files[2]}" "$@"
}

owned_resources() {
  local mode=$1 kind ids id labels
  local -a flags
  for kind in container network volume; do
    flags=(-q)
    if [[ "$kind" == container ]]; then flags+=(--all); fi
    ids=$(
      docker "$kind" ls "${flags[@]}" --filter "label=com.docker.compose.project=$project" || exit 1
      if [[ "$kind" != container ]]; then
        docker "$kind" ls "${flags[@]}" --filter "name=^${project}_"
      fi
    ) || return 1
    ids=$(sort -u <<<"$ids")
    if [[ "$mode" == empty && -n "$ids" ]]; then
      fail "unexpected $kind resources for $project"
      return 1
    fi
    for id in $ids; do
      labels=$(docker "$kind" inspect "$id") || return 1
      if ! jq -e --arg project "$project" --arg token "$token" '
        .[0] | (.Config.Labels // .Labels) |
        .["com.docker.compose.project"] == $project and .["org.alnahda.s1-12-run"] == $token
      ' >/dev/null <<<"$labels"; then
        fail "refusing cleanup of $kind $id: ownership mismatch"
        return 1
      fi
    done
  done
}

verify_config() {
  compose --profile maintenance config --format json | jq -e --arg p "$project" --arg t "$token" --arg root "$scratch" \
    --arg template "$PWD/ops/staging/nginx.conf.template" '
    def owned: .labels["org.alnahda.s1-12-run"] == $t;
    .name == $p
    and all((.networks // {} | to_entries[]), (.volumes // {} | to_entries[]);
      (.value | owned) and (.value.external != true) and .value.name == ($p + "_" + .key))
    and all(.services[];
      owned and (has("container_name") | not) and (has("network_mode") | not)
      and (.privileged != true) and (has("pid") | not) and (has("ipc") | not)
      and ((.ports // []) | length) == 0
      and all(.volumes[]?; .type == "volume" or .type == "tmpfs" or
        (.type == "bind" and .read_only == true and
          ((.source | startswith($root + "/")) or .source == $template))))
  ' >/dev/null
}

safe_down() {
  check_root || { fail 'invalid cleanup directory'; return 1; }
  if [[ ! -f "$scratch/config.sha256" ]]; then
    [[ ! -f "$scratch/cleanup-armed" ]] || { fail 'missing recorded configuration'; return 1; }
    rm -rf -- "$scratch"
    return
  fi
  IFS= read -r token < "$scratch/owner.token" || return 1
  [[ "$token" =~ ^[0-9a-f]{32}$ ]] || { fail 'invalid ownership token'; return 1; }
  local daemon
  daemon=$(docker info --format '{{.ID}}') || return 1
  [[ -n "$daemon" && "$daemon" == "$(<"$scratch/docker.id")" ]] ||
    { fail 'Docker daemon differs from recorded run'; return 1; }
  sha256sum --check --status "$scratch/config.sha256" || { fail 'Compose inputs changed'; return 1; }
  verify_config || { fail 'invalid effective Compose configuration'; return 1; }
  owned_resources owned || return 1
  compose down --volumes --remove-orphans || return 1
  owned_resources empty || return 1
  local image_id
  image_id=$(docker image ls -q --filter "reference=$project-erp-app") || return 1
  if [[ -n "$image_id" ]]; then
    [[ "$(docker image inspect --format '{{index .Config.Labels "org.alnahda.s1-12-run"}}' "$project-erp-app")" == "$token" ]] ||
      { fail 'refusing to remove an image owned by another run'; return 1; }
    docker image rm "$project-erp-app" >/dev/null || return 1
  fi
  rm -rf -- "$scratch"
}

finish() {
  local test_status=$? cleanup_status=0 service ledger_file
  trap - EXIT
  trap '' INT TERM
  if [[ -n "${S1_12_EVIDENCE_DIR:-}" && -z "${exercise:-}" && -f "$scratch/cleanup-armed" ]]; then
    if mkdir -p -- "$S1_12_EVIDENCE_DIR"; then
      for ledger_file in a04a-cut a04a-retry a04b-first a04b-retry; do
        if [[ -f "$scratch/$ledger_file.json" ]]; then
          cp -- "$scratch/$ledger_file.json" "$S1_12_EVIDENCE_DIR/$ledger_file-ledger.json" || cleanup_status=1
        fi
      done
      for service in a b; do
        if compose exec -T "client-$service" test -d /tmp/s1-12-state 2>/dev/null; then
          mkdir -p -- "$S1_12_EVIDENCE_DIR/client-$service" &&
          compose exec -T "client-$service" tar -C /tmp/s1-12-state -cf - . |
            tar -xf - -C "$S1_12_EVIDENCE_DIR/client-$service" || cleanup_status=1
        fi
      done
    else
      cleanup_status=1
    fi
  fi
  local down_status=0
  safe_down || down_status=$?
  if ((down_status != 0)); then cleanup_status=$down_status; fi
  printf 'S1-12: tests exit=%s cleanup exit=%s duration=%ss\n' "$test_status" "$cleanup_status" "$((SECONDS-started))"
  if ((test_status != 0)); then exit "$test_status"; fi
  exit "$cleanup_status"
}

if [[ ${1:-} == --lifecycle-check ]]; then
  exec python3 - "$0" <<'PYTHON'
from pathlib import Path
import os
import signal
import subprocess
import tempfile
import time
import sys

script = sys.argv[1]
parent = os.environ.get("RUNNER_TEMP") or os.environ.get("TMPDIR") or "/tmp"

with tempfile.TemporaryDirectory(prefix="s112-check-", dir=parent) as evidence:
    metadata = Path(evidence)
    processes = []
    def fixture(case, result):
        pointer = metadata / case
        process = subprocess.Popen(["bash", script, "--exercise", str(pointer), str(result)])
        processes.append((process, pointer))
        expires = time.monotonic() + 60
        while time.monotonic() < expires and process.poll() is None:
            if pointer.exists() and pointer.read_text().strip():
                root = Path(pointer.read_text().strip())
                if (root / "fixture-ready").exists():
                    return process, pointer, root
            time.sleep(0.1)
        raise RuntimeError(f"Fixture {case} failed to start: {process.poll()}")

    def checked(process, root, expected):
        assert process.wait(timeout=40) == expected
        assert not root.exists()

    def release(root):
        (root / "fixture-release").touch()

    def foreign_case(case, test_status):
        process, pointer, root = fixture(case, test_status)
        foreign = subprocess.check_output([
            "docker", "volume", "create", "--label",
            f"com.docker.compose.project={root.name}", f"{root.name}_foreign",
        ], text=True).strip()
        try:
            release(root)
            assert process.wait(timeout=40) == (test_status or 1)
            assert root.exists()
            subprocess.run(["docker", "volume", "inspect", foreign],
                           check=True, stdout=subprocess.DEVNULL)
        finally:
            # The fixture itself created this synthetic sentinel by explicit name.
            subprocess.run(["docker", "volume", "rm", foreign], check=True)
        subprocess.run(["bash", script, "--cleanup", str(pointer)], check=True)
        assert not root.exists()

    try:
        success, _, ok_root = fixture("success", 0)
        failure, _, fail_root = fixture("failure", 37)
        assert ok_root != fail_root
        assert (ok_root / "fixture-port").read_text() == (fail_root / "fixture-port").read_text() == ""
        release(ok_root)
        checked(success, ok_root, 0)
        subprocess.run(["docker", "volume", "inspect", f"{fail_root.name}_pgdata"],
                       check=True, stdout=subprocess.DEVNULL)
        release(fail_root)
        checked(failure, fail_root, 37)
        for sig, status in [(signal.SIGINT, 130), (signal.SIGTERM, 143)]:
            interrupted, _, root = fixture(sig.name, 0)
            interrupted.send_signal(sig)
            checked(interrupted, root, status)
        foreign_case("cleanup-only-failure", 0)
        foreign_case("combined-failure", 37)
    finally:
        for process, pointer in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=40)
            subprocess.run(["bash", script, "--cleanup", str(pointer)], check=True)
print("S1-12 lifecycle: concurrent isolation, success, failed test, INT, TERM, foreign guard and retry verified")
PYTHON
fi

if [[ ${1:-} == --cleanup ]]; then
  [[ $# == 2 ]] || { fail 'usage: --cleanup <run pointer>'; exit 2; }
  [[ -f "$2" ]] || exit 0
  IFS= read -r scratch < "$2"
  [[ -d "$scratch" ]] || exit 0
  check_root || { fail 'invalid cleanup pointer'; exit 1; }
  exec 9>"$scratch/run.lock"
  flock -n 9 || { fail 'test process still owns this run'; exit 1; }
  safe_down
  exit
fi
exercise=
if [[ ${1:-} == --exercise && $# == 3 && $3 =~ ^(0|37)$ ]]; then
  exercise=$3
elif [[ $# != 0 && ( $# != 2 || $1 != --run-pointer ) ]]; then
  fail 'usage: [--run-pointer FILE] | --lifecycle-check'
  exit 2
fi

parent=$(realpath -e -- "${RUNNER_TEMP:-${TMPDIR:-/tmp}}")
name_token=$(openssl rand -hex 12)
scratch="$parent/s112-$name_token"
mkdir -- "$scratch"
check_root
exec 9>"$scratch/run.lock"
flock -n 9
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
token=$(openssl rand -hex 16)
printf '%s\n' "$token" > "$scratch/owner.token"
printf 'S1-12: project=%s cleanup directory=%s\n' "$project" "$scratch"
if (($#)); then
  (set -o noclobber; printf '%s\n' "$scratch" > "$2")
fi

# All input, key and trust material belongs to this invocation.
mkdir -- "$scratch/tls" "$scratch/nssdb"
ssl="$scratch/tls"
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -sha256 \
  -subj '/CN=AlNahda S1-12 ephemeral test CA' -addext 'basicConstraints=critical,CA:TRUE' \
  -keyout "$scratch/ca.key" -out "$ssl/ca.crt" >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes -sha256 -subj '/CN=erp.s112.test' \
  -keyout "$ssl/server.key" -out "$scratch/server.csr" >/dev/null 2>&1
printf '%s\n' 'subjectAltName=DNS:erp.s112.test,DNS:owner.s112.test' \
  'extendedKeyUsage=serverAuth' 'basicConstraints=critical,CA:FALSE' > "$scratch/server.ext"
openssl x509 -req -in "$scratch/server.csr" -CA "$ssl/ca.crt" -CAkey "$scratch/ca.key" \
  -CAcreateserial -days 2 -sha256 -extfile "$scratch/server.ext" -out "$ssl/server.crt" >/dev/null 2>&1
certutil -N --empty-password -d "sql:$scratch/nssdb"
certutil -A -d "sql:$scratch/nssdb" -n 'AlNahda S1-12 test CA' -t 'C,,' -i "$ssl/ca.crt"
chmod 0755 "$ssl" "$scratch/nssdb"
chmod 0644 "$ssl/"*.crt "$scratch/nssdb/"*
owned_resources empty
# Pull/build before starting the isolated project. The office uses preloaded images.
postgres_ref=postgres:18
nginx_ref=nginx:1.28-alpine
for image in "$postgres_ref" "$nginx_ref"; do
  if ! docker image inspect "$image" >/dev/null 2>&1; then docker pull "$image"; fi
done
postgres_image=$(docker image inspect --format '{{index .RepoDigests 0}}' "$postgres_ref")
nginx_image=$(docker image inspect --format '{{index .RepoDigests 0}}' "$nginx_ref")
[[ "$postgres_image" =~ @sha256:[0-9a-f]{64}$ && "$nginx_image" =~ @sha256:[0-9a-f]{64}$ ]]
cat > "$scratch/test.env" <<ENV
ERP_STAGING_APP_IMAGE=$project-erp-app
ERP_STAGING_DB_IMAGE=$postgres_image
ERP_S1_12_NGINX_IMAGE=$nginx_image
ERP_S1_12_TOKEN=$token
ERP_S1_12_ROOT=$scratch
ERP_STAGING_INTERNAL_HOST=erp.s112.test
ERP_STAGING_OWNER_HOST=owner.s112.test
ERP_WEB_PORT=0
ERP_DB_NAME=erp_staging
ERP_DB_USER=erp_owner
ERP_DB_PASSWORD=$(openssl rand -hex 24)
ERP_WEB_DB_USER=erp_runtime
ERP_WEB_DB_PASSWORD=$(openssl rand -hex 24)
ERP_DJANGO_SECRET_KEY=$(openssl rand -hex 32)
ERP_PRINT_MEMORY_LIMIT=1g
S1_12_PASSWORD_A=$(openssl rand -hex 24)
S1_12_PASSWORD_B=$(openssl rand -hex 24)
ENV
docker info --format '{{.ID}}' > "$scratch/docker.id"
sha256sum "${checksum_files[@]}" "$scratch/test.env" > "$scratch/config.sha256"
touch "$scratch/cleanup-armed"
verify_config
if [[ -n "$exercise" ]]; then
  compose create --no-build db
  proxy_fixture=$(compose run --no-deps -d proxy sleep 180)
  docker port "$proxy_fixture" > "$scratch/fixture-port"
  [[ ! -s "$scratch/fixture-port" ]]
  owned_resources owned
  touch "$scratch/fixture-ready"
  deadline=$((SECONDS+80))
  until [[ -f "$scratch/fixture-release" ]]; do
    (( SECONDS < deadline )) || exit 124
    sleep .2
  done
  exit "$exercise"
fi
[[ -z "$(docker image ls -q --filter "reference=$project-erp-app")" ]]
docker build --label "org.alnahda.s1-12-run=$token" --tag "$project-erp-app" .
docker image inspect --format 'S1-12 app image ID={{.Id}}' "$project-erp-app"
printf 'S1-12 commit=%s postgres=%s nginx=%s\n' "$(git rev-parse HEAD)" "$postgres_image" "$nginx_image"

set -a
source "$scratch/test.env"
set +a
compose up -d --no-build --wait db
compose exec -T -e "ERP_WEB_DB_USER=$ERP_WEB_DB_USER" -e "ERP_WEB_DB_PASSWORD=$ERP_WEB_DB_PASSWORD" db \
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
compose exec -T db psql -At -U "$ERP_DB_USER" -d "$ERP_DB_NAME" -c 'SHOW server_version'
compose run --rm --no-deps -T -e "S1_12_PASSWORD_A=$S1_12_PASSWORD_A" -e "S1_12_PASSWORD_B=$S1_12_PASSWORD_B" migrate \
  python manage.py shell --verbosity 0 -c 'from tests.s1_12_acceptance import seed; seed()'
compose up -d --no-build --wait web printer proxy client-a client-b outside
owned_resources owned
for service in db web printer proxy outside client-a client-b; do
  container=$(compose ps -q "$service")
  [[ -n "$container" ]]
  ports=$(docker port "$container")
  [[ -z "$ports" ]]
done
printf 'S1-12: no test container publishes a host port\n'

probe() {
  local actor=$1 action=$2
  shift 2
  local password_key="S1_12_PASSWORD_${actor^^}"
  compose exec -T -e "ERP_STAGING_INTERNAL_HOST=$ERP_STAGING_INTERNAL_HOST" \
    -e "ERP_STAGING_OWNER_HOST=$ERP_STAGING_OWNER_HOST" -e "$password_key=${!password_key}" \
    "client-$actor" python -m tests.s1_12_acceptance "$action" --actor "$actor" "$@"
}

ledger() {
  local scenario=$1 expected=$2 evidence_name=$3 counts first=''
  case "$evidence_name" in a04a-cut|a04a-retry|a04b-first|a04b-retry) ;; *) return 2 ;; esac
  case "$scenario" in S1-12-A04a|S1-12-A04b) ;; *) return 2 ;; esac
  counts=$(compose run --rm --no-deps -T migrate python manage.py shell --verbosity 0 \
    -c "from tests.s1_12_acceptance import ledger; ledger('$scenario')") || return 1
  printf '%s\n' "$counts"
  if [[ "$scenario" == S1-12-A04b ]]; then
    first=$(compose exec -T client-a cat /tmp/s1-12-state/a04b-first.json) || return 1
  fi
  jq -e --argjson expected "$expected" --argjson first "${first:-null}" '
    ([.documents, .attachments, .events, .receipts] | all(. == $expected))
    and ($expected == 0 or (.receipt_state == ["COMPLETED"] and .receipt_status == [303] and .lines == 2))
    and ($first == null or ($first.status == 303 and .receipt_locations == [$first.location]
      and .attachment_sha256 == [$first.attachment_sha256] and .attachment_names == [$first.attachment_name]))
  ' >/dev/null <<<"$counts" || return 1
  printf '%s\n' "$counts" > "$scratch/$evidence_name.json"
}

escape() {
  local service=$1 expectation=$2
  compose exec -T "$service" python - "$expectation" <<'PY'
import sys
from urllib.request import urlopen

try:
    with urlopen("http://outside:8080/", timeout=4) as response:
        reachable = response.status == 200
except OSError:
    reachable = False
print(f"Synthetic outside reachable={reachable}, expected={sys.argv[1]}")
assert reachable == (sys.argv[1] == "yes")
PY
}

for service in web client-a client-b; do
  escape "$service" yes
  docker network disconnect "${project}_${service//-/_}_egress" "$(compose ps -q "$service")"
  escape "$service" no
done

result=0
record() {
  local criterion=$1
  shift
  local status=0
  ( set -e; "$@" ) || status=$?
  printf 'S1-12 %s exit=%s\n' "$criterion" "$status"
  if ((status && result == 0)); then result=$status; fi
}

login_both() { probe a login && probe b login; }
data_flow() {
  probe a store &&
  probe b store &&
  compose exec -T client-a cat /tmp/s1-12-state/flow-a.json > "$scratch/flow-a.json" &&
  compose exec -T client-b cat /tmp/s1-12-state/flow-b.json > "$scratch/flow-b.json" &&
  compose exec -T client-b sh -c 'cat > /tmp/s1-12-state/peer-a.json' < "$scratch/flow-a.json" &&
  compose exec -T client-a sh -c 'cat > /tmp/s1-12-state/peer-b.json' < "$scratch/flow-b.json" &&
  probe a cross --peer /tmp/s1-12-state/peer-b.json &&
  probe b cross --peer /tmp/s1-12-state/peer-a.json
}

printer_assets() {
  local printer
  printer=$(compose ps -q printer) || return 1
  docker inspect "$printer" | jq -e --arg network "${project}_printing" '
    .[0].NetworkSettings.Networks | keys == [$network]
  ' >/dev/null || return 1
  docker network inspect "${project}_printing" | jq -e '.[0].Internal' >/dev/null || return 1
  compose exec -T printer python - <<'PYTHON'
import hashlib
import json
from apps.printing import renderer, worker

assert worker._assets_ready()
paths = [worker.ASSETS / "bootstrap.rtl.min.css", worker.ARABIC_FONT]
print(json.dumps({
    "renderer_origin": renderer.ORIGIN,
    "printing_network_internal": True,
    "local_assets_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
}))
PYTHON
}

printing() {
  local locker status=0
  compose exec -T printer python -c '
import fcntl
import time
from pathlib import Path
with open("/var/lib/erp/print/render.lock", "a+") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    Path("/tmp/s112-lock-ready").touch()
    deadline = time.monotonic() + 45
    while not Path("/tmp/s112-lock-done").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("test print lock release timed out")
        time.sleep(.1)
' &
  locker=$!
  if wait_file printer /tmp/s112-lock-ready; then
    probe a print --expect 429 || status=$?
  else
    status=1
  fi
  compose exec -T printer touch /tmp/s112-lock-done || status=$?
  wait "$locker" || status=$?
  ((status == 0)) &&
  printer_assets &&
  probe a print --expect 200 &&
  probe b print --expect 200 &&
  compose exec -T client-a cat /tmp/s1-12-state/print-a.pdf > "$scratch/print-a.pdf" &&
  compose exec -T client-b cat /tmp/s1-12-state/print-b.pdf > "$scratch/print-b.pdf" &&
  python3 -m tests.s1_12_acceptance inspect-pdf --actor a --pdf "$scratch/print-a.pdf" &&
  python3 -m tests.s1_12_acceptance inspect-pdf --actor b --pdf "$scratch/print-b.pdf"
}

wait_file() {
  local service=$1 path=$2 end=$((SECONDS+35))
  until compose exec -T "$service" test -f "$path"; do
    ((SECONDS < end)) || return 1
    sleep .25
  done
}

before_save() {
  local browser status=0 disconnected=0
  local client
  client=$(compose ps -q client-a) || return 1
  probe a a04a &
  browser=$!
  if wait_file client-a /tmp/s1-12-state/a04a-ready.json &&
      docker network disconnect "$project"_lan "$client"; then
    disconnected=1
    if compose exec -T client-a touch /tmp/s1-12-state/a04a-cut &&
        wait_file client-a /tmp/s1-12-state/a04a-failed.json &&
        probe b login && ledger S1-12-A04a 0 a04a-cut; then
      :
    else
      status=1
    fi
  else
    status=1
  fi
  if ((disconnected)); then
    docker network connect "$project"_lan "$client" || status=1
  fi
  compose exec -T client-a touch /tmp/s1-12-state/a04a-resume || status=1
  wait "$browser" || status=$?
  if ((status == 0)); then ledger S1-12-A04a 1 a04a-retry || status=1; fi
  return "$status"
}

after_save() {
  local browser status=0
  probe a a04b &
  browser=$!
  if wait_file client-a /tmp/s1-12-state/a04b-first.json &&
      ledger S1-12-A04b 1 a04b-first &&
      compose exec -T client-a touch /tmp/s1-12-state/a04b-commit; then
    :
  else
    status=1
  fi
  wait "$browser" || status=$?
  ledger S1-12-A04b 1 a04b-retry || status=1
  cmp -s "$scratch/a04b-first.json" "$scratch/a04b-retry.json" || status=1
  if ((status == 0)); then
    compose exec -T client-a cat /tmp/s1-12-state/a04b-result.json |
      jq -e --slurpfile db "$scratch/a04b-retry.json" '
        .reference == $db[0].references[0] and .retry.location == $db[0].receipt_locations[0]
      ' >/dev/null || status=1
  fi
  return "$status"
}

restart_flow() {
  local service container deadline
  for service in db web printer; do
    container=$(compose ps -q "$service") || return 1
    compose restart "$service" || return 1
    deadline=$((SECONDS+45))
    until [[ "$(docker inspect --format '{{.State.Health.Status}}' "$container")" == healthy ]]; do
      ((SECONDS < deadline)) || return 1
      sleep 1
    done
  done
  probe a reopen && probe b reopen && probe a print --expect 200
}

record A01 login_both
record A02 data_flow
record A03 printing
record A04a before_save
record A04b after_save
permissions() { probe a permissions && probe b permissions; }
record A06 permissions
record A07 restart_flow
exit "$result"
