# OPS-AUTO-01 — PR governance and controlled delivery

State: implementation proposal awaiting PR checks and independent review. This document does **not** change the approved ERP SRS or authorize S1-05 implementation.

## Verified repository constraints (2026-11-25)

- `main` is the default branch of the private repository.
- Existing `.github/workflows/ci.yml` checks secrets, PostgreSQL 18, Django schema and migrations, and Django tests.
- GitHub repository reports `allow_auto_merge=false`. The repository rulesets endpoint returned HTTP 403 with a GitHub plan-upgrade message; branch-protection read returned 403 for the integration.
- Therefore **no technical main-branch protection is presently verified**. A green PR check alone cannot prevent direct pushes or unauthorized merging.

## Required workflow

1. Read the applicable approved design and `AGENTS.md`. Prepare a cohesive development batch with each work item's exact scope, dependencies, tests and rollback. Do not start unresolved financial or security behavior.
2. Codex works in a dedicated branch and opens a Pull Request with all template sections populated and accurate risk declarations.
3. Two distinct kinds of automated checks run: existing Django/PostgreSQL/secret CI and the deterministic `PR Governance / PR metadata and risk` gate. Each is necessary, neither is a code-review approval.
4. An **independent reviewer**, separate from the coding session, reviews the diff, tests, migrations, permissions, finance impacts and approved business rules. GitHub review approval must be recorded by another identity, not asserted in the PR description. This service/identity **is not configured by OPS-AUTO-01**.
5. Until an enforceable GitHub branch protection/ruleset or equivalent privileged merge gate is proven, `main` merges remain operator-controlled and **auto-merge stays disabled**. Passing PR checks alone never grants merger permission.
6. If a finding materially changes accepted business rules, security policy or posted accounting, stop and escalate for explicit business approval. Normal implementation corrections stay within the authorized batch.

## Automatic path classification

`.github/ci/pr_governance.py` classifies changed file paths to make some risks visible. It requires the PR body to declare governance, schema migration, access/security and financial sensitivity. Path inspection is incomplete by design: a `no` flag is **not** proof that a PR is safe or that domain behavior was unaffected.

## Rollout acceptance

- [ ] Open a draft PR containing the rollout changes; confirm both `CI / PostgreSQL 18` and `PR Governance / PR metadata and risk` actually execute and succeed.
- [ ] Test the governance gate rejects a missing description and misdeclared risk flags.
- [ ] Establish an independent reviewer identity and process; demonstrate a real reviewed PR. Do not pretend a self-review is independent.
- [ ] Demonstrate enforced main protection or equivalent gate before enabling unattended merges.
- [ ] Confirm no standing production credentials, deploy permissions or secrets are granted to the coding agent.
- [ ] Only then authorize the next Sprint 1 development batch as a unit.

## Rollback

Before merge: close the PR and delete its branch. After a reviewed merge: revert the PR commit(s), retaining audit history. This rollout does not alter database schema or production data.
