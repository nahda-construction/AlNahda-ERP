"use strict";
// Offline synthetic probe only. Never read PR data, login tokens, or host files.
const fs = require("node:fs");
const os = require("node:os");
const cp = require("node:child_process");
const assert = require("node:assert/strict");

const status = fs.readFileSync("/proc/self/status", "utf8");
assert.equal(process.getuid(), 10001, "unexpected uid");
assert.match(status, /^CapEff:\s+0+$/m, "effective Linux capabilities present");
assert.match(status, /^NoNewPrivs:\s+1$/m, "no-new-privileges not enforced");
assert.match(status, /^Seccomp:\s+2$/m, "seccomp filtering not active");
assert.deepEqual(Object.keys(os.networkInterfaces()).sort(), ["lo"], "non-loopback interface present");
assert.ok(!fs.existsSync("/var/run/docker.sock"), "Docker socket exposed");
assert.ok(!fs.existsSync("/run/desktop/mnt/host"), "host mount exposed");
assert.ok(!fs.existsSync("/mnt/c"), "Windows mount exposed");
for (const key of ["GH_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN",
                   "SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS"]) {
    assert.ok(!(key in process.env), "unexpected credential or session variable");
}
assert.equal(process.env.HOME, "/tmp/reviewer");
assert.equal(process.env.CODEX_HOME, "/tmp/reviewer/.codex");
assert.ok(!fs.existsSync(process.env.CODEX_HOME), "existing Codex profile exposed");
const mounts = fs.readFileSync("/proc/self/mountinfo", "utf8").trim().split("\n");
const root = mounts.find(line => line.split(" ")[4] === "/");
assert.ok(root && root.split(" ")[5].split(",").includes("ro"), "root filesystem not read-only");
const tmp = mounts.find(line => line.split(" ")[4] === "/tmp");
assert.ok(tmp && tmp.split(" - ")[1].startsWith("tmpfs "), "/tmp not tmpfs");
assert.ok(tmp.split(" ")[5].split(",").includes("nosuid"), "tmpfs lacks nosuid");
assert.ok(tmp.split(" ")[5].split(",").includes("nodev"), "tmpfs lacks nodev");
const mem = fs.readFileSync("/sys/fs/cgroup/memory.max", "utf8").trim();
const pids = fs.readFileSync("/sys/fs/cgroup/pids.max", "utf8").trim();
assert.ok(/^\d+$/.test(mem) && Number(mem) <= 512 * 1024 * 1024, "memory limit absent");
assert.ok(/^\d+$/.test(pids) && Number(pids) <= 64, "process limit absent");
fs.mkdirSync(process.env.HOME, {mode: 0o700});
fs.writeFileSync(process.env.HOME + "/fixture.txt", "SYNTHETIC_ONLY", {mode: 0o600});
assert.equal(fs.readFileSync(process.env.HOME + "/fixture.txt", "utf8"), "SYNTHETIC_ONLY");
const version = cp.execFileSync("codex", ["--version"], {encoding: "utf8", timeout: 15000, maxBuffer: 4096}).trim();
assert.match(version, /^(?:codex-cli|codex) 0\.172\.0$/i, "unexpected Codex version");
console.log("CODEX_VERSION=" + version);
console.log("OFFLINE_CONTAINMENT_PROBE_OK");
console.log("LIVE_REVIEW=BLOCKED");
