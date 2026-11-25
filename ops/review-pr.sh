#!/usr/bin/env bash
# Compatibility entrypoint. Never execute the candidate PR or auto-update the runner.
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
install_dir="$HOME/.local/share/alnahda-review-runner"
runner="$install_dir/local_review_runner.py"
bare_repo="$install_dir/repo.git"
if [[ ! -f $runner || ! -d $bare_repo ]]; then
    echo "Install the reviewed pinned local runner first. Do not run an unreviewed PR checkout." >&2
    exit 2
fi
args=(--repo "$bare_repo" --pr "$1" --once)
if [[ ${2:-} != --local-only ]]; then
    args+=(--publish)
fi
exec python3 "$runner" "${args[@]}"
