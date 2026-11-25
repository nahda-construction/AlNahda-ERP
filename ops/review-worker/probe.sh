#!/usr/bin/env bash
# Disposable offline containment probe. Run from a reviewed, pinned revision only.
set -euo pipefail
umask 077
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
[[ $# -eq 0 ]] || { echo "This probe accepts no arguments" >&2; exit 2; }
command -v docker >/dev/null
command -v timeout >/dev/null
# Require the local Unix-socket context; refuse remote Docker hosts.
[[ -z ${DOCKER_HOST:-} ]] || { echo "Unset DOCKER_HOST before running" >&2; exit 2; }
endpoint="$(docker context inspect "$(docker context show)" --format '{{.Endpoints.docker.Host}}')"
[[ "$endpoint" == unix://* ]] || { echo "Local Linux Docker socket required" >&2; exit 2; }
[[ "$(docker info --format '{{.OSType}}')" == linux ]] || { echo "Linux containers required" >&2; exit 2; }
work="$(mktemp -d)"
name="alnahda-probe-$(basename "$work" | tr -cd '[:alnum:]')"
created=0
cleanup() {
    if [[ "$created" == 1 ]]; then docker rm -f "$name" >/dev/null 2>&1 || true; fi
    rm -rf -- "$work"
}
trap cleanup EXIT
# Copy ONLY the reviewed probe build inputs into a separate context.
cp -- "$here/Dockerfile" "$here/probe.js" "$work/"
timeout 300 docker build --pull --iidfile "$work/image.id" "$work"
image="$(cat "$work/image.id")"
[[ "$image" =~ ^sha256:[a-f0-9]{64}$ ]] || { echo "Invalid image ID" >&2; exit 2; }
echo "PROBE_IMAGE=$image"
# No host mount, auth cache, credentials, Git, network, Docker socket or data.
docker create --name "$name" --pull never --network none --read-only \
    --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges=true \
    --pids-limit 64 --memory 512m --memory-swap 512m --cpus 1 \
    --tmpfs /tmp:rw,nosuid,nodev,size=64m,mode=1777 \
    --log-driver none "$image" >/dev/null
created=1
# Inspect the actual container before starting, not merely the CLI options.
python3 - "$name" <<'PY'
import json, subprocess, sys
data = json.loads(subprocess.check_output(["docker", "inspect", sys.argv[1]]))[0]
host = data["HostConfig"]
assert host["NetworkMode"] == "none"
assert host["ReadonlyRootfs"] and not host["Privileged"]
assert not host["Binds"] and not host["Devices"] and not host["DeviceRequests"]
assert not data["Mounts"] or all(m["Type"] == "tmpfs" and m["Destination"] == "/tmp" for m in data["Mounts"])
assert host["PidMode"] != "host" and host["IpcMode"] != "host"
assert host["Memory"] == 512 * 1024 * 1024 and host["MemorySwap"] == host["Memory"]
assert host["PidsLimit"] == 64 and host["NanoCpus"] == 1_000_000_000
assert "ALL" in host["CapDrop"] and not host["CapAdd"]
assert data["Config"]["User"] == "10001:10001"
assert host["LogConfig"]["Type"] == "none"
print("CONTAINER_CONFIG_OK")
PY
timeout 60 docker start -a "$name"
[[ "$(docker inspect --format '{{.State.ExitCode}}' "$name")" == 0 ]] || exit 1
echo "OFFLINE_PROBE_COMPLETE; LIVE REVIEW REMAINS BLOCKED"
