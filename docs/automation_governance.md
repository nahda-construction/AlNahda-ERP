# OPS-AUTO-01 — PR governance and controlled delivery

State: proposed changes in PR #3; no main merge has been authorized. This document does **not** change approved ERP business rules or authorize S1-05.

## Verified repository limitations (2026-11-25)

- `main` is the default branch of a private repository. The GitHub API reports `protected=false`; no enforceable branch protection is currently verified.
- Existing `.github/workflows/ci.yml` checks secrets, PostgreSQL 18, Django migrations and Django tests.
- GitHub reports `allow_auto_merge=false`. Rulesets for this private repository were rejected with a plan restriction. Do not change visibility or buy a plan without owner consent.
- No green CI result or local preflight script prevents direct pushes or unchecked merges into unprotected `main`.

## Trust boundary for the governance gate

- `.github/workflows/pr-governance.yml` uses **`pull_request_target`**, which reads the workflow from the trusted base branch (`main`).
- It checks out **only `github.event.pull_request.base.sha`**, never the proposed PR revision, and executes only the base revision's `.github/ci/pr_governance.py`. Token permissions are read-only (`contents: read`, `pull-requests: read`); no PR code or artifacts are executed.
- The trusted checker reads the event payload from `GITHUB_EVENT_PATH`, verifies the live head SHA, base SHA and description before and after fetching paginated changed-file metadata, and verifies the count and uniqueness of current filenames. It rejects stale snapshots and incomplete results. It logs the PR number, head SHA, base SHA and SHA-256 of the body for later preflight comparison. Both previous and current rename paths count. The live API is not atomic; preflight must still recheck the current revision and body digest before a controlled merge.
- It tracks comments and fenced code in one parsing pass, ignoring comment-bearing lines conservatively and treating comment delimiters within fenced logs as literal. It excludes HTML comments and fenced code examples from metadata parsing, and refuses missing visible risk-mitigation explanations, repeated required sections, or duplicate/conflicting declarations. Path-detected risks require `yes`; authors may also declare additional risks the heuristic misses. Path classification is a heuristic warning, not proof of absence of business risk.
- `.github/workflows/governance-tests.yml` separately tests the proposed validator on ordinary `pull_request` events. **These unit tests are not the trusted governance check.**
- A `pull_request_target` check ordinarily attaches to the **base** branch commit, not necessarily the PR head SHA. Merge preflight tooling must not rely on finding this check in the PR-head check-runs list. Until end-to-end evidence is in place, the operator must inspect the latest trusted workflow run for that PR.

### Bootstrap limitation for PR #3

The secure `pull_request_target` workflow is **new** and is not yet present on `main`. It cannot be treated as an enforceable trusted gate on PR #3 itself. The PR requires separate diff review and manual controlled approval. After a carefully reviewed merge, verify the trusted governance workflow against a subsequent PR (including a deliberately malformed PR) **before** relying on it in the delivery process.

## Delivery policy

1. Establish the development batch scope and the controlling requirements in `AGENTS.md`.
2. Codex writes the batch on dedicated branches, with objective tests and explicit risks and rollback.
3. Run existing PostgreSQL/Django/secret CI, regression tests and the trusted PR metadata gate as applicable.
4. Run an advisory second-pass Codex review, then obtain a **real review from a different GitHub reviewer identity**. A Codex comment is not a GitHub `APPROVED` review.
5. Do not automatically merge before technical enforcement of required checks and reviews is demonstrated. Until that happens, the operator owns every merge.
6. Do not start financial posting or external owner-access work with unresolved FIN/SEC controls; escalate business-rule changes for owner approval.

## Rollout criteria

- [x] PR #3 opened, original PostgreSQL/Django/secret CI passed.
- [x] PR #3 had an advisory Codex review; it identified P1 and three P2 findings.
- [x] Changes prepared to address trusted execution, rename handling, risk explanations and HTML comments.
- [x] Second advisory review of `784c13b` reported three P2 findings; changes prepared for conservative risk declarations, unique flags and fenced code parsing.
- [x] Third advisory review of `1946742` identified stale-revision evidence and literal HTML comments in fenced logs; fixes and regression cases have been added.
- [ ] The **updated** PR #3 regression and PostgreSQL CI runs pass, and fresh advisory review finds no blockers.
- [ ] PR #3 is manually reviewed before merging; do not mistake old untrusted governance results for the secure gate.
- [ ] A later PR demonstrates a real trusted `pull_request_target` run and rejection of invalid metadata.
- [ ] A different GitHub review identity and actual enforceable main protection/merge control are verified.
- [ ] No standing production secrets or deployment privileges are granted to Codex.

## Rollback

Close the PR before merging. If already merged, revert the reviewed commits. No financial data or database schema is changed by this operations-only PR.
