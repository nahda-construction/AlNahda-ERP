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
- GitHub's ordinary read-only PR test workflow executes this offline probe as a separate job. Its pass does not prove WSL/Docker Desktop isolation on the operator's machine. Local one-shot verification was completed successfully on 2026-11-25, as reported by the operator: `CONTAINER_CONFIG_OK`, `CODEX_VERSION=codex-cli 0.172.0`, `OFFLINE_CONTAINMENT_PROBE_OK`, `LIVE_REVIEW=BLOCKED`, `OFFLINE_PROBE_COMPLETE`. Tested source revision: `bfc42527103ee9b38628e70122f525d05918543e`; local image ID: `sha256:c2077d2321332e3b288616c5204b372c6bc791b7f7354a103a2629dd212aa695`. The CODEX_HOME alias warning is consistent with the intentionally absent profile; the probe exited successfully. This is operator-provided evidence of the tested controls, not a comprehensive security certification.
- The base image digest is pinned to the one observed in the first CI build; the built image ID identifies the local test only. The first CI build verified Codex version and container configuration but failed because the non-root user could not read the copied probe file; COPY --chmod=0444 corrects that without adding privileges. Before authenticated Stage B2, verify the pinned base image and Codex package/build provenance; verify the effective tool policy; enforce an outbound network policy permitting required OpenAI authentication/inference only, with no GitHub/LAN/host access; use a dedicated minimal credential store. Device-code authentication requires operator consent and browser sign-in. Never copy the existing host auth cache or share tokens here.
- Stage B2 (authenticated synthetic inference), Stage B3 (approved real-diff scope), and Report Controller are not enabled. No continuous poller or publication is restored.

## Stage B2 — authentication prerequisite

- The offline probe is complete on CI and the operator's WSL. Do not use the offline image as an authenticated review worker or attach the host's existing Codex home.
- Use ChatGPT device-code login in a new isolated profile (official docs: https://learn.chatgpt.com/docs/auth). The account owner must enable device-code login in ChatGPT security settings and personally complete the device authorization when a reviewed online worker is ready. Do not paste the device code, token, or auth file into chat, GitHub or logs. No API key or extra subscription is authorized.
- Before initiating OAuth, implement and verify the restricted egress path and credential storage; the offline result does not validate the online network boundary. OpenAI's documented Codex inference connection includes HTTPS/WSS to chatgpt.com; the exact authentication/inference destinations must be validated rather than assuming the entire published general-purpose domain list is necessary. Official network reference: https://help.openai.com/en/articles/9247338-network-recommendations-for-chatgpt-errors-on-web-and-apps.
- First authenticated inference uses a fixed synthetic fixture only. No model invocation, real PR input, report publication, or unattended polling is enabled by this evidence update.

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

CI runs syntax checks and no-network regression tests, including bounded local subprocess output. Offline worker probe passed on GitHub and local WSL; authenticated worker, online network policy, credentials and publication controls remain unverified. No background process is installed. Delete the dedicated preparation directory if desired. Before merge, close the PR; after merge, revert reviewed commits. No database migration or ERP posting is involved.
