#!/usr/bin/env python3
"""Read-only GitHub merge preflight for AlNahda ERP.

This tool DOES NOT merge or protect branches. It is a second check for an
operator-controlled workflow until GitHub enforces required checks/reviews.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

REPO = "nahda-construction/AlNahda-ERP"
REQUIRED_CHECKS = ("PostgreSQL 18", "PR metadata and risk")


def get_json(endpoint: str) -> dict | list:
    response = subprocess.run(
        ["gh", "api", "-X", "GET", endpoint],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(response.stdout)


def get_reviews(pr_number: int) -> list[dict]:
    reviews: list[dict] = []
    for page in range(1, 21):
        batch = get_json(
            f"repos/{REPO}/pulls/{pr_number}/reviews?per_page=100&page={page}"
        )
        if not isinstance(batch, list):
            raise RuntimeError("Unexpected reviews API response")
        reviews.extend(batch)
        if len(batch) < 100:
            return reviews
    raise RuntimeError("Review list too large to verify safely")


def assess(
    pr: dict,
    main_branch: dict,
    checks: dict,
    reviews: list[dict],
) -> list[str]:
    blockers: list[str] = []
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    sha = head.get("sha")
    author = (pr.get("user") or {}).get("login", "").casefold()

    if pr.get("state") != "open" or pr.get("draft") is not False:
        blockers.append("PR must be open and not a draft")
    if base.get("ref") != "main":
        blockers.append("PR target must be main")
    if not re.fullmatch(r"[0-9a-f]{40}", str(sha or ""), flags=re.I):
        blockers.append("Missing or malformed PR head SHA")
    if base.get("sha") != ((main_branch.get("commit") or {}).get("sha")):
        blockers.append("PR base is not the current main commit")
    if pr.get("mergeable") is not True:
        blockers.append("GitHub has not confirmed this PR is mergeable")

    if not isinstance(checks, dict) or not isinstance(checks.get("check_runs"), list):
        blockers.append("Cannot verify GitHub check runs")
        check_runs = []
    elif checks.get("total_count", len(checks["check_runs"])) > len(checks["check_runs"]):
        blockers.append("Incomplete check run pagination; no merge clearance")
        check_runs = checks["check_runs"]
    else:
        check_runs = checks["check_runs"]

    for required in REQUIRED_CHECKS:
        matches = [
            run for run in check_runs
            if run.get("name") == required
            and run.get("head_sha") == sha
            and (run.get("app") or {}).get("slug") == "github-actions"
        ]
        latest = max(matches, key=lambda run: run.get("id", 0), default=None)
        if latest is None:
            blockers.append(f"Required check missing: {required}")
        elif latest.get("status") != "completed" or latest.get("conclusion") != "success":
            blockers.append(f"Required check not successful: {required}")

    latest_substantive: dict[str, dict] = {}
    for review in sorted(reviews, key=lambda r: (r.get("submitted_at") or "", r.get("id", 0))):
        user = (review.get("user") or {}).get("login", "").casefold()
        state = review.get("state")
        if user and user != author and state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest_substantive[user] = review
    if any(r.get("state") == "CHANGES_REQUESTED" for r in latest_substantive.values()):
        blockers.append("An independent reviewer requested changes")
    eligible = [
        r for r in latest_substantive.values()
        if r.get("state") == "APPROVED" and r.get("commit_id") == sha
    ]
    if not eligible:
        blockers.append("No fresh APPROVED GitHub review from an identity other than author")

    body = pr.get("body") or ""
    for flag, target in (("Financial-sensitive", "FIN"),
                         ("Access/security-sensitive", "SEC")):
        if re.search(rf"(?mi)^{re.escape(flag)}:\s*yes\s*$", body):
            blockers.append(f"{target} sign-off must be handled separately before merge")

    return blockers


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].isdigit() or int(argv[1]) < 1:
        print("Usage: python ops/merge_preflight.py PR_NUMBER", file=sys.stderr)
        return 2
    number = int(argv[1])
    try:
        pr = get_json(f"repos/{REPO}/pulls/{number}")
        main_branch = get_json(f"repos/{REPO}/branches/main")
        head_sha = (pr.get("head") or {}).get("sha")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", str(head_sha or "")):
            raise RuntimeError("Malformed PR head SHA")
        checks = get_json(f"repos/{REPO}/commits/{head_sha}/check-runs?per_page=100")
        reviews = get_reviews(number)
        blockers = assess(pr, main_branch, checks, reviews)
    except (OSError, subprocess.CalledProcessError, ValueError, RuntimeError, KeyError) as exc:
        print(f"BLOCKED: unable to verify GitHub state: {exc}", file=sys.stderr)
        return 2
    print(f"PR #{number} @ {head_sha}")
    if blockers:
        for item in blockers:
            print(f"BLOCKED: {item}")
        print("No merge authorized. Branch protection is not enforced.")
        return 1
    print("Preflight PASS: checks and distinct GitHub reviewer evidence verified.")
    print("This does NOT enforce branch protection or execute a merge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
