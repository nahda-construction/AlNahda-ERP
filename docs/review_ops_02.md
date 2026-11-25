# OPS-AUTO-02 — Local advisory review runner and merge preflight

**Status:** Stage A (local, one-shot review-input preparation) proposed. Independent review of `f7e62605` found two P1 and five P2 issues. Live model execution, automated publication, and unattended polling are now blocked in code. This is not a completed automation system.

## Stage A — bounded local preparation

- A reviewed pinned script outside the development checkout uses a dedicated bare clone, restricted to same-repository PR #3 and #5 targeting main. It fetches and verifies exact head/base SHAs; binds the local candidate filename to those SHAs and the PR body digest; and revalidates the revision before atomic private file creation.
- Reads stdout and stderr as bytes with streaming caps, timeout and subprocess-group termination. Preserves raw diff bytes and provides an escaping helper for invalid UTF-8, CR and literal backslashes. Binary changes and oversized/empty diffs require separate scope review.
- Explicit github.com API host; no PR comments or other write operations. Transient revision changes exit 75, independently of operational errors.
- Per-file limit for git subprocesses, clone-size pre/post checks, free-space guard and local candidate retention cap. These are not a hard aggregate filesystem quota. No unattended fetch/polling until such a quota is enforced.
- Only local one-shot preparation (`--once`) is supported. The compatibility wrapper prepares a local candidate instead of invoking Codex or publishing. No candidate is sent to any model. A local candidate is not a completed code review.

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

CI runs syntax checks and no-network regression tests, including bounded local subprocess output. Actual isolated worker, WSL runtime, credentials and publication controls remain unverified. No background process is installed. Delete the dedicated preparation directory if desired. Before merge, close the PR; after merge, revert reviewed commits. No database migration or ERP posting is involved.
