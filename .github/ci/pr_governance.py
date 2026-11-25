#!/usr/bin/env python3
"""Read-only PR metadata gate, intended to run from trusted main.

Use only in pull_request_target with a base-commit checkout. No PR code is
executed. A passing result is not independent review or branch protection.
"""
from __future__ import annotations

import hashlib
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


def parse_sections(body: str) -> tuple[dict[str, str], list[str]]:
    """Parse visible top-level headings and declarations outside fenced code."""
    sections: dict[str, list[str]] = {}
    names: list[str] = []
    current: str | None = None
    fence: tuple[str, int] | None = None
    in_comment = False
    for line in body.splitlines():
        if fence is not None:
            closing = re.fullmatch(r" {0,3}([`~]+)[ \t]*", line)
            if closing and all(ch == fence[0] for ch in closing.group(1)) and len(closing.group(1)) >= fence[1]:
                fence = None
            continue
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if not in_comment and opening and (opening.group(1)[0] == "~" or "`" not in opening.group(2)):
            marker = opening.group(1)
            fence = (marker[0], len(marker))
            continue
        # Comments inside fenced logs are literal, not Markdown comments.
        # Do not join fragments across lines or accept hidden declarations.
        if in_comment or "<!--" in line:
            remainder = line
            while remainder:
                if in_comment:
                    end = remainder.find("-->")
                    if end < 0:
                        break
                    in_comment = False
                    remainder = remainder[end + 3:]
                else:
                    start = remainder.find("<!--")
                    if start < 0:
                        break
                    in_comment = True
                    remainder = remainder[start + 4:]
            # Ignore comment-bearing lines conservatively; visible metadata
            # must be declared on its own line.
            continue
        match = re.fullmatch(r" {0,3}##[ \t]+(.+?)[ \t]*", line)
        if match:
            current = match.group(1).strip().casefold()
            names.append(current)
            sections.setdefault(current, []).append("")
        elif current is not None:
            sections[current].append(line)
    return {heading: "\n".join(lines).strip() for heading, lines in sections.items()}, names


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


def api_json(url: str, token: str):
    req = request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "AlNahda-ERP-PR-governance",
    })
    with request.urlopen(req, timeout=20) as response:
        return json.load(response)


def revision(pr: dict) -> tuple[str, str, str]:
    """Bind the verdict to exact head/base SHAs and the description."""
    try:
        head, base = pr["head"]["sha"], pr["base"]["sha"]
        body = pr["body"]
    except (KeyError, TypeError) as exc:
        raise ValueError("Malformed PR revision") from exc
    if not all(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha)
               for sha in (head, base)):
        raise ValueError("Malformed PR revision SHA")
    if body is None:
        body = ""
    if not isinstance(body, str):
        raise ValueError("Malformed PR body")
    return head, base, body


def changed_paths(repo: str, pr_number: str, token: str,
                  event_pr: dict) -> list[str]:
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", repo):
        raise ValueError("Malformed repository name")
    if not pr_number.isdecimal() or int(pr_number) < 1:
        raise ValueError("Malformed PR number")
    if not token:
        raise ValueError("Missing read-only GitHub token")
    if event_pr.get("number") != int(pr_number):
        raise ValueError("PR number mismatch")
    expected = revision(event_pr)
    url = f"https://api.github.com/repos/{repo}/pulls/{int(pr_number)}"
    before = api_json(url, token)
    if revision(before) != expected:
        raise ValueError("PR changed since event; require a fresh run")
    count = before.get("changed_files")
    if type(count) is not int or not 0 < count < 3000:
        raise ValueError("Cannot verify complete changed-file count")
    all_files: list[dict] = []
    for page in range(1, 31):
        batch = api_json(f"{url}/files?per_page=100&page={page}", token)
        if not isinstance(batch, list):
            raise ValueError("Malformed changed-files API response")
        all_files.extend(batch)
        if len(batch) < 100:
            break
    paths = paths_from_api_files(all_files)
    if len(all_files) != count or len({item["filename"] for item in all_files}) != count:
        raise ValueError("Incomplete or duplicate changed-file list")
    after = api_json(url, token)
    if (revision(after) != expected or after.get("changed_files") != count
            or after.get("updated_at") != before.get("updated_at")):
        raise ValueError("PR changed during file enumeration; require a fresh run")
    return paths


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
    sections, names = parse_sections(body)
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
        declarations = re.findall(
            rf"(?mi)^[ \t]*{re.escape(flag)}[ \t]*:([^\r\n]*)$", risks
        )
        if not declarations:
            issues.append(f"Risk declaration is missing: {flag}")
        elif len(declarations) != 1:
            issues.append(f"Duplicate risk declaration: {flag}")
        else:
            value = declarations[0].strip().casefold()
            if value not in ("yes", "no"):
                issues.append(f"Risk declaration must be yes or no: {flag}")
            elif expected[flag] and value != "yes":
                issues.append(f"Risk declaration mismatch: {flag} must be yes")
    return issues


def main() -> int:
    try:
        with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as event_file:
            event_pr = json.load(event_file)["pull_request"]
        head, base, body = revision(event_pr)
        number = os.getenv("PR_NUMBER", "")
        paths = changed_paths(
            os.getenv("PR_REPO", ""), number, os.getenv("GH_TOKEN", ""), event_pr,
        )
        print("Verified PR revision:", json.dumps({
            "pr": int(number), "head": head, "base": base,
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        }, sort_keys=True))
    except (error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"Cannot classify PR changes: {exc}", file=sys.stderr)
        return 2
    print("Changed paths (including rename sources):")
    for path in paths:
        print(f"  {json.dumps(path)}")
    print("Automatically classified risk flags:")
    for flag, value in expected_flags(paths).items():
        print(f"  {flag}: {'yes' if value else 'no'}")
    errors = validate(body, paths)
    if errors:
        for issue in errors:
            print(f"ERROR: {issue}", file=sys.stderr)
        return 1
    print("PR metadata gate passed; independent code review remains separate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
