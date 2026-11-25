#!/usr/bin/env python3
"""Fail-closed preparation stage for the local advisory review runner.

Real model execution and publication remain blocked pending verified isolation.
No PR code, merge, approval, workflow dispatch, Gemini, or deployment action.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import selectors
import shutil
import signal
import subprocess
import tempfile
import time

REPO = "nahda-construction/AlNahda-ERP"
REMOTE = {"https://github.com/" + REPO, "https://github.com/" + REPO + ".git",
          "git@github.com:" + REPO + ".git"}
MAX_DIFF = 180_000
MAX_API = 4_000_000
MAX_STDERR = 256_000
MAX_PACK_FILE = 64 * 1024 * 1024
MAX_CLONE_BYTES = 256 * 1024 * 1024
MIN_DISK_FREE = 512 * 1024 * 1024
PILOT_PRS = frozenset({3, 5})
SHA = re.compile(r"[0-9a-f]{40}")


class RevisionChanged(RuntimeError):
    """A normal concurrent PR update, not an operational failure."""


def run_bytes(args, *, cwd=None, input_text=None, env=None, timeout=60,
              max_output=MAX_API, limit_file=False):
    """Bound both output streams as bytes while the subprocess is running."""
    def limit():
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_PACK_FILE, MAX_PACK_FILE))

    deadline = time.monotonic() + timeout
    # Use a temporary input file so a blocked writer cannot deadlock output reads.
    with tempfile.TemporaryFile() as source:
        if input_text is not None:
            source.write(input_text.encode("utf-8"))
        source.seek(0)
        with subprocess.Popen(args, cwd=cwd, stdin=source, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env, start_new_session=True,
                              preexec_fn=limit if limit_file else None) as proc:
            out, err = bytearray(), bytearray()
            try:
                with selectors.DefaultSelector() as sel:
                    sel.register(proc.stdout, selectors.EVENT_READ, (out, max_output))
                    sel.register(proc.stderr, selectors.EVENT_READ, (err, MAX_STDERR))
                    while sel.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(args[0], timeout)
                        ready = sel.select(remaining)
                        if not ready:
                            raise subprocess.TimeoutExpired(args[0], timeout)
                        for key, _ in ready:
                            data = os.read(key.fd, 65536)
                            if not data:
                                sel.unregister(key.fileobj)
                                continue
                            buf, cap = key.data
                            if len(buf) + len(data) > cap:
                                raise ValueError(f"{args[0]} output exceeds configured byte limit")
                            buf.extend(data)
                if proc.wait(timeout=max(0.01, deadline - time.monotonic())):
                    raise RuntimeError(f"{args[0]} exited {proc.returncode}")
            except BaseException:
                # Kill the entire subprocess group, including fetch helpers.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                raise
            return bytes(out)


def run(args, **kwargs):
    return run_bytes(args, **kwargs).decode("utf-8", errors="strict")


def git(root, *args):
    return run(["git", "-C", str(root), *args], limit_file=True)


def git_bytes(root, *args, max_output=MAX_DIFF):
    return run_bytes(["git", "-C", str(root), *args], max_output=max_output,
                     limit_file=True)


def api(endpoint):
    return json.loads(run(["gh", "api", "--hostname", "github.com",
                           "-X", "GET", endpoint]))


def check_disk(root):
    # Per-file subprocess limit + pre/post clone size guard. This is not
    # a filesystem quota; aggregate growth during fetch remains possible.
    total = 0
    for directory, _, files in os.walk(root, followlinks=False):
        for name in files:
            total += (Path(directory) / name).lstat().st_size
            if total > MAX_CLONE_BYTES:
                raise ValueError("Dedicated clone exceeds size guard")
    if shutil.disk_usage(root).free < MIN_DISK_FREE:
        raise ValueError("Insufficient disk headroom")


def snapshot(pr, number):
    if pr.get("number") != number or pr.get("state") != "open" or pr.get("draft") is not False:
        raise ValueError("PR not eligible (must be open, non-draft)")
    head, base = pr["head"], pr["base"]
    if (base.get("ref") != "main" or (base.get("repo") or {}).get("full_name") != REPO
            or (head.get("repo") or {}).get("full_name") != REPO):
        raise ValueError("Only same-repository PRs to main are eligible")
    h, b = head.get("sha"), base.get("sha")
    if not all(isinstance(x, str) and SHA.fullmatch(x) for x in (h, b)):
        raise ValueError("Invalid commit SHA")
    body = pr.get("body")
    if body is not None and not isinstance(body, str):
        raise ValueError("Invalid PR description")
    digest = hashlib.sha256((body or "").encode()).hexdigest()
    return h, b, digest


def diff_for(root, number, revision):
    head, base, _ = revision
    check_disk(root)
    git(root, "-c", "core.hooksPath=/dev/null", "fetch", "--no-tags", "origin", "main")
    check_disk(root)
    if git(root, "rev-parse", "FETCH_HEAD").strip() != base:
        raise RevisionChanged("Base moved during fetch")
    git(root, "-c", "core.hooksPath=/dev/null", "fetch", "--no-tags", "origin", f"pull/{number}/head")
    check_disk(root)
    if git(root, "rev-parse", "FETCH_HEAD").strip() != head:
        raise RevisionChanged("Head moved during fetch")
    common = ("-c", "core.hooksPath=/dev/null", "diff", "--no-ext-diff",
              "--no-textconv", "--no-color", "--no-renames")
    stat = git_bytes(root, *common, "--numstat", "-z", f"{base}...{head}", "--")
    if any(item.startswith(b"-\t-\t") for item in stat.split(b"\0")):
        raise ValueError("Binary changes are unreviewed; manual scope review required")
    raw = git_bytes(root, *common, f"{base}...{head}", "--")
    if not raw.strip() or len(raw) > MAX_DIFF:
        raise ValueError("Empty or oversized diff: manual scope review required")
    return raw


def visible_diff(raw):
    """Preserve byte distinctions: escape backslashes, CR and invalid UTF-8."""
    text = raw.decode("utf-8", errors="surrogateescape")
    parts = []
    for ch in text:
        value = ord(ch)
        if ch == "\\":
            parts.append("\\\\")
        elif ch == "\r":
            parts.append("\\r")
        elif 0xDC80 <= value <= 0xDCFF:
            parts.append(f"\\x{value - 0xDC00:02x}")
        elif value < 32 and ch not in "\n\t":
            parts.append(f"\\x{value:02x}")
        else:
            parts.append(ch)
    return "".join(parts)


def prepare_one(root, number, state):
    """Prepare a local immutable candidate. No external model or comment."""
    endpoint = f"repos/{REPO}/pulls/{number}"
    pr = api(endpoint)
    if pr.get("state") != "open" or pr.get("draft"):
        return "skipped: closed/draft"
    rev = snapshot(pr, number)
    head, base, digest = rev
    dest = state / f"pr-{number}-{head}-{base}-{digest}.diff"
    if dest.is_file():
        return "already prepared"
    raw = diff_for(root, number, rev)
    if sum(p.stat().st_size for p in state.glob("pr-*.diff")) + len(raw) > 16 * 1024 * 1024:
        raise ValueError("Local candidate retention limit reached; archive reviewed candidates")
    if snapshot(api(endpoint), number) != rev:
        raise RevisionChanged("PR changed during preparation")
    # Atomic private file; no model output, credentials or diff sent to GitHub.
    with tempfile.NamedTemporaryFile(dir=state, prefix=".candidate-", delete=False) as tmp:
        name = Path(tmp.name)
        try:
            tmp.write(raw)
            tmp.flush()
            os.fsync(tmp.fileno())
            os.replace(name, dest)
        finally:
            name.unlink(missing_ok=True)
    return "prepared locally; model review blocked pending isolation"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--pr", type=int, action="append", required=True,
                        help="Explicit allowlist; repeat for each PR")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--publish", action="store_true",
                        help="Disabled until publication policy and isolation are verified")
    args = parser.parse_args()
    if not set(args.pr) <= PILOT_PRS:
        parser.error("Pilot is restricted to PR #3 and #5; expansion requires reviewed change")
    if args.publish:
        parser.error("Automatic publication blocked pending isolation and exact-report approval policy")
    if not args.once:
        parser.error("Only one-shot local preparation is allowed until a hard disk quota and isolated worker are verified")
    root = args.repo.resolve(strict=True)
    if git(root, "rev-parse", "--is-bare-repository").strip() != "true":
        raise SystemExit("Runner requires its own trusted bare clone; do not use a development checkout")
    if git(root, "remote", "get-url", "origin").strip() not in REMOTE:
        raise SystemExit("Unexpected origin; refusing to run")
    state = Path.home() / ".local" / "state" / "alnahda-review-runner"
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state / "runner.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another local runner is active")
        failed = False
        retry = False
        for number in sorted(set(args.pr)):
            try:
                print(f"PR #{number}: {prepare_one(root, number, state)}", flush=True)
            except RevisionChanged as exc:
                print(f"PR #{number}: RETRY ({exc})", flush=True)
                retry = True
            except (OSError, ValueError, RuntimeError, KeyError, TypeError,
                    subprocess.TimeoutExpired) as exc:
                print(f"PR #{number}: BLOCKED ({type(exc).__name__}): {exc}", flush=True)
                failed = True
        return 1 if failed else 75 if retry else 0


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
