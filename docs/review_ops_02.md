# OPS-AUTO-02 — Local advisory review runner and merge preflight

**Status:** Stage A (local, one-shot review-input preparation) proposed. Independent review of `f7e62605` found two P1 and five P2 issues. Live model execution, automated publication, and unattended polling are now blocked in code. This is not a completed automation system.

## Stage A — bounded local preparation

- A reviewed pinned script outside the development checkout uses a dedicated bare clone, restricted to same-repository PR #3 and #5 targeting main. It fetches and verifies exact head/base SHAs; binds the local candidate filename to those SHAs and the PR body digest; and revalidates the revision before atomic private file creation.
- Reads stdout and stderr as bytes with streaming caps, timeout and subprocess-group termination. Preserves raw diff bytes and provides an escaping helper for invalid UTF-8, CR and literal backslashes. Binary changes and oversized/empty diffs require separate scope review.
- Explicit github.com API host; no PR comments or other write operations. Transient revision changes exit 75, independently of operational errors.
- Per-file limit for git subprocesses, clone-size pre/post checks, free-space guard and local candidate retention cap. These are not a hard aggregate filesystem quota. No unattended fetch/polling until such a quota is enforced.
- Only local one-shot preparation (`--once`) is supported. The compatibility wrapper prepares a local candidate instead of invoking Codex or publishing. No candidate is sent to any model. A local candidate is not a completed code review.

## Stage B1 — disposable offline worker probe

- `ops/review-worker/Dockerfile` builds from the official Node Linux image and installs exactly `@openai/codex@0.172.0` (the version that passed the local CLI compatibility test). No host auth file, GitHub token, application files or PR diff enters the build context. Only the two probe build files are copied. Build-time package downloads require Internet, but the runtime probe is completely offline.
- `probe.sh` requires the local Linux Docker Unix socket, creates a disposable non-root container using its built image ID, with no network, no host mounts, read-only root, dropped capabilities, no new privileges, default seccomp, 512 MiB RAM without added swap, one CPU, 64 processes, 64 MiB temporary filesystem, and no retained Docker log. Inspect the actual configuration before starting. Remove the container on exit.
- `probe.js` verifies runtime UID, capabilities, privilege/seccomp status, loopback-only networking, mount types, absence of common host mounts/credential variables, cgroup-v2 memory/process bounds and exact Codex CLI version. It creates only a synthetic temporary fixture and does NOT invoke a model or authenticate. A passing result is not proof against every container escape or configuration risk.
- GitHub's ordinary read-only PR test workflow executes this offline probe as a separate job. Its pass does not prove WSL/Docker Desktop isolation on the operator's machine. Local one-shot verification is still necessary.
- The probe base image tag is mutable; its built image ID identifies the local test only. Before authenticated Stage B2, pin and verify the base image digest and Codex package/build provenance; verify the effective tool policy; enforce an outbound network policy permitting required OpenAI authentication/inference only, with no GitHub/LAN/host access; use a dedicated minimal credential store. Device-code authentication requires operator consent and browser sign-in. Never copy the existing host auth cache or share tokens here.
- Stage B2 (authenticated synthetic inference), Stage B3 (approved real-diff scope), and Report Controller are not enabled. No continuous poller or publication is restored.

## Stage B — required before unattended review

- Use a separately reviewed isolated worker with a pinned verified Codex executable/build, dedicated authentication/profile state, no host home or GitHub credentials, no session bus, no repository checkout, bounded resources, and enforced filesystem/network policy. Verify effective tool configuration and containment on actual WSL/Docker runtime. Merely accepting flags or using read-only sandbox does not establish confidentiality.
- Validate approved review input scope and data-handling policy before any model transfer. Pilot with synthetic input first. Do not silently expand to real ERP data or source.
- Keep raw generated review text private. Do not automatically publish raw text to GitHub. Any exact report to be published requires explicit review/approval under a defined policy; consider deterministic status-only publication for unattended operation, separately review its design and disclosure risks. Recheck current revision immediately before any future publication and account for residual API race and multiple publishers.
- A hard quota for the dedicated fetch/candidate storage and a verified single publisher are required before unattended operation. No new paid subscription or self-hosted Actions runner.

## Independent review disposition

| Finding | Disposition |
| --- | --- |
| P1 model text publication | Publication removed; approval policy still required before any restoration. |
| P1 model isolation | Model invocation removed; isolated worker not implemented/verified. |
| P2 output/disk bounds | Streaming limits and per-file/size guards implemented; hard aggregate disk quota pending. |
| P2 byte preservation/binary | Raw bytes preserved; binary diffs rejected; presentation escaping helper added. |
| P2 stale report/multi-host | No publication; preparation checks revision; future publication race must be addressed. |
| P2 GitHub host | API hostname pinned; no publication path. |
| P2 normal revision churn | Dedicated retry outcome; not an operational failure. |

## Remaining merge controls (not resolved by this pilot)

- The existing merge preflight is not ready for use: trusted `pull_request_target` evidence is base-attached, not an ordinary head check. Require exact PR/head/base/body evidence from the trusted workflow and validate freshness, main ancestry and actual authorized reviewer identity. A temporary CLI interlock refuses to emit PASS until this verifier is implemented; legacy assessment unit tests alone are not merge authorization.
- PR #3 is the trusted-gate bootstrap and cannot rely on a workflow not yet on main. Neither a Codex comment nor a second model is a separate GitHub APPROVED identity. No branch protection or automatic merge is enabled; no direct pushes to main.
- Preserve FIN/SEC approval and all approved business decisions. No production data or credentials are permitted in review input.

## OPS-AUTO-03 — approved Gemini constraints

Owner approved the following constraints: free Antigravity CLI only; advisory independent review; selected non-confidential changes only; no GitHub credentials, direct repository access, write or merge privileges; no client/financial/credential data; no additional paid subscription; exhausted quota must not halt development. Do not call Gemini from the Codex session. No automatic Gemini integration or private-code transfer is enabled by this PR. Its separate runner and privacy/isolation controls remain pending.

## Tests and rollback

CI runs syntax checks and no-network regression tests, including bounded local subprocess output. Offline worker probe is implemented; actual WSL runtime, authenticated worker, credentials and publication controls remain unverified. No background process is installed. Delete the dedicated preparation directory if desired. Before merge, close the PR; after merge, revert reviewed commits. No database migration or ERP posting is involved.
