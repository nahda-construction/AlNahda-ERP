#!/usr/bin/env bash
# Standalone AI second-pass, not a GitHub approval or a branch protection rule.
set -euo pipefail
umask 077

if [[ $# -lt 1 || $# -gt 2 || ! $1 =~ ^[0-9]+$ || $1 == 0 ]]; then
    echo "Usage: bash ops/review-pr.sh PR_NUMBER [--local-only]" >&2
    exit 2
fi
if [[ $# -eq 2 && $2 != --local-only ]]; then
    echo "Unsupported option: $2" >&2
    exit 2
fi
for program in git gh codex; do
    command -v "$program" >/dev/null || { echo "Missing: $program" >&2; exit 2; }
done

pr=$1
repo_root=$(git rev-parse --show-toplevel)
repo=$(gh repo view --json nameWithOwner --jq '.nameWithOwner')
if [[ $repo != "nahda-construction/AlNahda-ERP" ]]; then
    echo "Wrong GitHub repository: $repo" >&2
    exit 2
fi

base=$(gh pr view "$pr" --repo "$repo" --json baseRefName --jq '.baseRefName')
head=$(gh pr view "$pr" --repo "$repo" --json headRefOid --jq '.headRefOid')
state=$(gh pr view "$pr" --repo "$repo" --json state --jq '.state')
if [[ $base != main || $state != OPEN || ! $head =~ ^[a-fA-F0-9]{40}$ ]]; then
    echo "Expected an open PR against main with a valid head SHA" >&2
    exit 2
fi

# Git and the GitHub CLI are run by the Ubuntu operator, not by Codex.
git -C "$repo_root" fetch --no-tags origin main
git -C "$repo_root" fetch --no-tags origin "pull/$pr/head"
if [[ $(git -C "$repo_root" rev-parse FETCH_HEAD) != "$head" ]]; then
    echo "PR commit changed during fetch; refusing to review an unverified SHA" >&2
    exit 1
fi

worktree=$(mktemp -d "${TMPDIR:-/tmp}/alnahda-pr-review.XXXXXXXX")
report=$(mktemp "${TMPDIR:-/tmp}/alnahda-pr-report.XXXXXXXX")
model_output=$(mktemp "${TMPDIR:-/tmp}/alnahda-pr-output.XXXXXXXX")
model_errors=$(mktemp "${TMPDIR:-/tmp}/alnahda-pr-errors.XXXXXXXX")
cleanup() {
    git -C "$repo_root" worktree remove --force "$worktree" >/dev/null 2>&1 || true
    rm -rf -- "$worktree"
    rm -f -- "$report" "$model_output" "$model_errors"
}
trap cleanup EXIT

git -C "$repo_root" worktree add --detach "$worktree" "$head" >/dev/null

# A fresh Codex review session; no write permissions and no GitHub token needed.
# Repository text and PR content are untrusted inputs to the reviewer.
if ! (cd "$worktree" && NO_COLOR=1 codex review --base origin/main >"$model_output" 2>"$model_errors"); then
    echo "Codex review failed; no GitHub comment posted." >&2
    cat "$model_errors" >&2
    exit 1
fi
if [[ ! -s $model_output ]]; then
    echo "Codex returned no review text; no GitHub comment posted." >&2
    exit 1
fi

{
    printf '## AI second-pass review (advisory only)\n\n'
    printf 'PR: #%s  |  Head: `%s`\n\n' "$pr" "$head"
    printf '**Not a GitHub APPROVED review.** This is a fresh Codex review session, not a separate GitHub reviewer identity. Do not merge solely on this report.\n\n'
    cat "$model_output"
} >"$report"

if [[ ${2:-} == --local-only ]]; then
    cat "$report"
else
    gh pr comment "$pr" --repo "$repo" --body-file "$report"
    echo "Posted advisory review report to $repo PR #$pr (head $head)."
fi
