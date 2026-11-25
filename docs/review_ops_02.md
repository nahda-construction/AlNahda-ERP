# OPS-AUTO-02 — Independent review evidence and safe merge preflight

**Status:** Proposed controls in a separate PR. No branch protection has been enabled and no automatic merger is created.

## What is available without extra credentials

- GitHub Actions already runs Django/PostgreSQL 18/secret checks; OPS-AUTO-01 PR #3 introduces a separate metadata/risk check.
- Ubuntu has an authenticated GitHub CLI and a Codex CLI. The new `ops/review-pr.sh` script can run a **fresh, separate Codex review session**, isolated in a temporary detached Git worktree against the exact PR head SHA.
- That second Codex session is **advisory AI review**, not a distinct GitHub user or an independently enforced approval. It posts a clearly labelled PR comment via the operator's existing GitHub CLI credentials; it does not approve or merge.
- `ops/merge_preflight.py` checks the live GitHub head SHA, main base, required successful check runs, and fresh `APPROVED` GitHub reviews from an identity different from the author. It blocks when there is a newer request for changes, or a FIN/SEC-sensitive declaration without separate sign-off.

## Operator-run review

From the **normal Ubuntu terminal, outside the Codex sandbox**, after these scripts are in the local checkout:

```bash
cd /home/engah/code/AlNahda-ERP
bash ops/review-pr.sh 3
```

This command posts the report to PR #3 and requires the existing `gh` and `codex` login. Use `--local-only` to print without posting. It does not require Docker or new GitHub privileges.

After the reviewer identity and required workflows are set up:

```bash
python3 ops/merge_preflight.py 3
```

The preflight exits nonzero if the repository is not demonstrably ready. It **never performs a merge**, regardless of its result.

## Still outstanding

1. Configure and verify a **different GitHub reviewer identity/service** recording a real review. A Copilot review was requested for PR #3 as an availability test, but no completed/pending review was verifiable at the time of this draft. Copilot normally leaves `COMMENT` reviews, which do not satisfy `APPROVED` by default.
2. GitHub private repository is on a plan that does not currently expose rulesets; branch protection has not been verified/enforced. Until this is resolved, checks and preflight are **procedural gates**, not a GitHub platform-level prohibition against direct pushes. Auto-merge remains off.
3. Financial posting and owner-facing security still require the separately approved FIN/SEC gates specified in `AGENTS.md`.
4. Run `bash -n ops/review-pr.sh`, unit tests, and a live local Codex read-only review once this PR is checked out. Verify the posted comment is attributable to the correct PR SHA.
5. Do not treat OPS-AUTO-01 or OPS-AUTO-02 as authorization for S1-05 implementation; approve a coherent Sprint 1 batch scope and proceed under existing G3 instructions.

## Fail-safe rollback

Before merge: close the PR. After merge: revert the commits. No Django migration or database/data change.
