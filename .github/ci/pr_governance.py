#!/usr/bin/env python3
"""Fail-closed PR description and file-risk declaration gate.

This is a deterministic quality check, not an independent human or AI review.
It cannot protect an unprotected main branch from direct pushes or manual merges.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

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


def parse_sections(body: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            current = match.group(1).strip().casefold()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {heading: "\n".join(lines).strip() for heading, lines in sections.items()}


def changed_paths(base_sha: str) -> list[str]:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", base_sha):
        raise ValueError("Missing or malformed pull-request base SHA")
    data = subprocess.check_output(
        ["git", "diff", "--name-only", "-z", f"{base_sha}...HEAD"],
    )
    return [name for name in data.decode("utf-8").split("\0") if name]


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
    sections = parse_sections(body)
    for heading in HEADINGS:
        value = sections.get(heading, "")
        meaningful_lines = [
            line.strip() for line in value.splitlines()
            if line.strip() and not line.lstrip().startswith("<!--")
        ]
        if not meaningful_lines or all(
            line.casefold() in PLACEHOLDERS for line in meaningful_lines
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
    body = os.getenv("PR_BODY", "")
    base_sha = os.getenv("PR_BASE_SHA", "")
    try:
        paths = changed_paths(base_sha)
    except (subprocess.CalledProcessError, UnicodeDecodeError, ValueError) as exc:
        print(f"Cannot classify changes: {exc}", file=sys.stderr)
        return 2
    print("Changed files:")
    for path in paths:
        print(f"  {path}")
    print("Automatically classified risk flags:")
    for flag, value in expected_flags(paths).items():
        print(f"  {flag}: {'yes' if value else 'no'}")
    errors = validate(body, paths)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("PR metadata gate passed; independent code review remains separate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
