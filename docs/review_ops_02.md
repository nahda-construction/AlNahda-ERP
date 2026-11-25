# OPS-AUTO-02 — Local advisory review runner and merge preflight

**Status:** Local runner pilot proposed on PR #5, per owner approval to prioritize automation. It is not yet installed or verified on Ubuntu. No change to ERP business requirements, main protection, reviewer identity, or merge authority.

## Local runner scope

- Install a reviewed, commit-pinned copy of `ops/local_review_runner.py` outside the development checkout, using a separate trusted bare clone. Never auto-update from a PR or execute the proposed application code.
- Explicit PR-number allowlist, initially PR #3 and PR #5. Same-repository, non-draft, open PRs targeting main only. No arbitrary GitHub instructions or commands are interpreted.
- Fetch exact head and base objects, verify both SHAs, and compute the diff without checking out PR files, external diff drivers or textconv. Cap the input at 180,000 bytes; oversized or empty diffs require manual scope review.
- Invoke a fresh Codex CLI session from an empty temporary directory, using `exec`, read-only sandbox and no approval escalation. Send the diff as data via stdin. Strip GitHub token and SSH-agent variables from the model subprocess. The user's existing Codex login remains necessary.
- Compare the live revision and PR description digest before publishing. The report is advisory, includes its head/base/body digest, and is not a GitHub APPROVED review. The marker is used only for deduplication, not as trusted evidence or authorization.
- Explicit `--publish` sends fenced, bounded advisory text to the same private PR through the operator's existing GitHub identity. Local-only mode uses `--once` without publish. Code fencing and prompt instructions are not a secret scanner.
- One local runner at a time, 120-second polling, five-minute backoff and suspension after three consecutive failures per PR. It runs only while the local Ubuntu/WSL environment is running; no new subscription or cloud-hosted runner is required.
- Never merge, approve, dispatch workflows, deploy, or run production tasks. A comment cannot trigger the execution of PR instructions. No self-hosted GitHub Actions runner is installed.

## Bootstrap and safety

The operator must install a pinned version after independent source review and CI. Do not execute unreviewed branch code. Pilot first with non-sensitive development-tooling diffs and verify Codex CLI flags, sandbox behavior, output and GitHub comment attribution on the actual machine. The read-only Codex sandbox does not by itself prove isolation from local home/configuration, MCP integrations, or other files. A dedicated restricted local account/profile or stronger OS isolation is required before expanding scope if the environment exposes secrets or additional tools. No raw credential output should be published; model-generated findings remain untrusted and must be reviewed.

The compatibility wrapper `ops/review-pr.sh` uses the pinned installed runner under `~/.local/share/alnahda-review-runner/`; it does not execute a candidate worktree. The earlier worktree-based helper is superseded for future use.

## Remaining merge controls (not resolved by this pilot)

- The existing merge preflight is not ready for use: trusted `pull_request_target` evidence is base-attached, not an ordinary head check. Require exact PR/head/base/body evidence from the trusted workflow and validate freshness, main ancestry and actual authorized reviewer identity. A temporary CLI interlock refuses to emit PASS until this verifier is implemented; legacy assessment unit tests alone are not merge authorization.
- PR #3 is the trusted-gate bootstrap and cannot rely on a workflow not yet on main. Neither a Codex comment nor a second model is a separate GitHub APPROVED identity. No branch protection or automatic merge is enabled; no direct pushes to main.
- Preserve FIN/SEC approval and all approved business decisions. No production data or credentials are permitted in review input.

## OPS-AUTO-03 — approved Gemini constraints

Owner approved the following constraints: free Antigravity CLI only; advisory independent review; selected non-confidential changes only; no GitHub credentials, direct repository access, write or merge privileges; no client/financial/credential data; no additional paid subscription; exhausted quota must not halt development. Do not call Gemini from the Codex session. No automatic Gemini integration or private-code transfer is enabled by this PR. Its separate runner and privacy/isolation controls remain pending.

## Tests and rollback

CI runs syntax checks and no-network mocked regression tests for the local runner and existing preflight. Actual Codex CLI, WSL runtime, credentials and comment publication require a controlled live pilot. Stop the local process to disable polling; delete the dedicated installation if desired. Before merge, close the PR; after merge, revert reviewed commits. No database migration or ERP posting is involved.
