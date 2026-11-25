#!/usr/bin/env bash
# Compatibility entrypoint for local preparation only. No model or publication.
set -euo pipefail
umask 077
if [[ $# -lt 1 || $# -gt 2 || ! $1 =~ ^[0-9]+$ || $1 == 0 || ( $# -eq 2 && $2 != --local-only ) ]]; then
    echo "Usage: bash ops/review-pr.sh PR_NUMBER [--local-only]" >&2
    exit 2
fi
install_dir="$HOME/.local/share/alnahda-review-runner"
runner="$install_dir/local_review_runner.py"
bare_repo="$install_dir/repo.git"
if [[ ! -f $runner || ! -d $bare_repo ]]; then
    echo "Install the reviewed pinned preparation runner first." >&2
    exit 2
fi
exec python3 "$runner" --repo "$bare_repo" --pr "$1" --once
