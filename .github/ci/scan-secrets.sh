#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || ( $1 != history && $1 != ci ) ]]; then
    printf 'Usage: bash .github/ci/scan-secrets.sh {history|ci}\n' >&2
    exit 2
fi

image=ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f
repo=$(git rev-parse --show-toplevel)
if [[ $(git rev-parse --is-shallow-repository) != false ]]; then
    printf 'Secret scanning requires a complete Git history.\n' >&2
    exit 2
fi

docker pull "$image"
scanner=(docker run --rm -i --network none
    --mount "type=bind,src=$repo,dst=/repo,readonly"
    --env GIT_CONFIG_COUNT=1 --env GIT_CONFIG_KEY_0=safe.directory
    --env GIT_CONFIG_VALUE_0=/repo "$image")

# A synthetic GitHub token is sent only through stdin.
status=0
printf 'ghp_%s\n' 'A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8' |
    "${scanner[@]}" stdin --redact=100 --exit-code 42 >/dev/null 2>&1 || status=$?
if [[ $status != 42 ]]; then
    printf 'Secret scanner self-test failed (exit %s).\n' "$status" >&2
    exit 2
fi

log_options='--all --reflog -m --full-history'
if [[ $1 == ci && ${GITHUB_EVENT_NAME:-} == pull_request ]]; then
    base=${CI_PR_BASE:?Missing pull request base SHA}
    git merge-base --is-ancestor "$base" HEAD
    log_options="-m --full-history $base..HEAD"
fi

status=0
"${scanner[@]}" git --redact=100 --exit-code 42 --ignore-gitleaks-allow \
    --log-opts "$log_options" /repo >/dev/null 2>&1 || status=$?
case "$status" in
    0) printf 'Secret scan and synthetic detection test passed.\n' ;;
    42) printf 'Potential secret detected. Review privately before continuing.\n' >&2; exit 1 ;;
    *) printf 'Secret scanner failed (exit %s).\n' "$status" >&2; exit 2 ;;
esac
