#!/usr/bin/env python3
"""Read-only PR metadata gate, intended to run from trusted main.

Use only in pull_request_target with a base-commit checkout. No PR code is
executed. A passing result is not independent review or branch protection.
"""
from __future__ import annotations

import json
import os
import re
import sys
from urllib import error, request

HEADINGS = (
    "work items",
    "authoritative references",
    "change summary",
    "verification",
    "risk and mitigations",
    "rollback",
)
FLAGS = (
    "Governance-sensitive",
    "Schema migration",
    "Access/security-sensitive",
    "Financial-sensitive",
)
PLACEHOLDERS = {"", "todo", "tbd", "n/a", "none", "pending", "...", "-"}


def strip_html_comments(body: str) -> str:
    """Remove complete and unterminated HTML comments."""
    return re.sub(r"<!--.*?(?:-->|\Z)", "", body, flags=re.DOTALL)


def parse_sections(body: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            current = match.group(1).strip().casefold()
            sections.setdefault(current, []).append("")
        elif current is not None:
            sections[current].append(line)
    return {heading: "\n".join(lines).strip() for heading, lines in sections.items()}


def paths_from_api_files(files: list[dict]) -> list[str]:
    """Include original and destination names of renamed files."""
    paths: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("Malformed changed-file item")
        current = item.get("filename")
        if not isinstance(current, str) or not current:
            raise ValueError("Changed-file item has no valid filename")
        paths.append(current)
        if item.get("status") == "renamed":
            old = item.get("previous_filename")
            if not isinstance(old, str) or not old:
                raise ValueError("Renamed file missing previous_filename")
            paths.append(old)
    return paths


def changed_paths(repo: str, pr_number: str, token: str) -> list[str]:
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", repo):
        raise ValueError("Malformed repository name")
    if not pr_number.isdecimal() or int(pr_number) < 1:
        raise ValueError("Malformed PR number")
    if not token:
        raise ValueError("Missing read-only GitHub token")
    all_files: list[dict] = []
    for page in range(1, 31):
        url = (f"https://api.github.com/repos/{repo}/pulls/{int(pr_number)}"
               f"/files?per_page=100&page={page}")
        req = request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "AlNahda-ERP-PR-governance",
        })
        with request.urlopen(req, timeout=20) as response:
            batch = json.load(response)
        if not isinstance(batch, list):
            raise ValueError("Malformed changed-files API response")
        all_files.extend(batch)
        if len(batch) < 100:
            if not all_files:
                raise ValueError("No changed files returned")
            return paths_from_api_files(all_files)
    raise ValueError("At least 3000 changed files: cannot verify complete list")


def expected_flags(paths: list[str]) -> dict[str, bool]:
    lowered = [path.casefold() for path in paths]
    return {
        "Governance-sensitive": any(
            p == "agents.md" or p.startswith((".github/", "ops/")) for p in lowered
        ),
        "Schema migration": any("/migrations/" in p and p.endswith(".py") for p in lowered),
        "Access/security-sensitive": any(
            re.search(r"(access|auth|permission|security|audit)", p) for p in lowered
        ),
        "Financial-sensitive": any(
            re.search(r"(financ|account|ledger|posting|cost|inventory|stock)", p)
            for p in lowered
        ),
    }


def validate(body: str, paths: list[str]) -> list[str]:
    issues: list[str] = []
    body = strip_html_comments(body)
    sections = parse_sections(body)
    names = [m.group(1).strip().casefold() for m in
             re.finditer(r"(?m)^##\s+(.+?)\s*$", body)]
    for heading in HEADINGS:
        if names.count(heading) > 1:
            issues.append(f"Duplicate PR section: ## {heading}")
        value = sections.get(heading, "")
        meaningful_lines = [
            line.strip() for line in value.splitlines()
            if line.strip() and not (
                heading == "risk and mitigations" and
                any(re.match(rf"^{re.escape(flag)}\s*:",
                             line.strip(), re.I) for flag in FLAGS)
            )
        ]
        if not meaningful_lines or all(
            line.strip().lstrip("-* ").casefold() in PLACEHOLDERS
            for line in meaningful_lines
        ):
            issues.append(f"Missing substantive PR section: ## {heading}")

    risks = sections.get("risk and mitigations", "")
    expected = expected_flags(paths)
    for flag in FLAGS:
        match = re.search(rf"(?mi)^{re.escape(flag)}:\s*(yes|no)\s*$", risks)
        correct = "yes" if expected[flag] else "no"
        if match is None:
            issues.append(f"Risk declaration is missing: {flag}: {correct}")
        elif match.group(1).casefold() != correct:
            issues.append(f"Risk declaration mismatch: {flag} must be {correct}")
    return issues


def main() -> int:
    try:
        paths = changed_paths(
            os.getenv("PR_REPO", ""),
            os.getenv("PR_NUMBER", ""),
            os.getenv("GH_TOKEN", ""),
        )
    except (error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Cannot classify PR changes: {exc}", file=sys.stderr)
        return 2
    print("Changed paths (including rename sources):")
    for path in paths:
        print(f"  {path}")
    print("Automatically classified risk flags:")
    for flag, value in expected_flags(paths).items():
        print(f"  {flag}: {'yes' if value else 'no'}")
    errors = validate(os.getenv("PR_BODY", ""), paths)
    if errors:
        for issue in errors:
            print(f"ERROR: {issue}", file=sys.stderr)
        return 1
    print("PR metadata gate passed; independent code review remains separate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
