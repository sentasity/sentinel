"""Publish a verified autofix: branch, commit, push, PR. Deterministic.

Runs in the workflow with the App installation token in GH_TOKEN. The
session never reaches this step's credentials; this step never edits code.

Env contract: AUTOFIX_WORKSPACE, AUTOFIX_SHORT_ID, AUTOFIX_DISPATCH_ID,
AUTOFIX_RELEASE_SHA, AUTOFIX_RUN_URL, AUTOFIX_BASE_BRANCH (default develop),
AUTOFIX_TARGET_REPO (required; the workflow sets it from repository variables).
Prints the final status on the last line; the workflow captures it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

FOOTER = (
    "\n\n---\n"
    "Opened by an unattended autofix run (dispatch `{dispatch_id}`). "
    "Root cause verified at release `{release_sha}`; fix written at develop "
    "`{develop_sha}`. Workflow run: {run_url}\n"
    "Review this as machine-written code: nothing here was seen by a human "
    "before this PR opened."
)


# Excludes the pipeline's own .autofix/ scratch dir at the top level only
# (git pathspec semantics). Shared by the diff check and the actual `git add`
# so the two can never drift apart.
EXCLUDE_AUTOFIX_DIR = ("--", ".", ":!.autofix")


def branch_name(short_id: str, dispatch_id: str) -> str:
    return f"autofix/{short_id.lower()}-{dispatch_id[:6]}"


def pr_title(short_id: str, summary_first_line: str) -> str:
    return f"[autofix] {summary_first_line} ({short_id})"


def pr_body(*, summary: str, dispatch_id: str, release_sha: str, develop_sha: str, run_url: str) -> str:
    return summary + FOOTER.format(
        dispatch_id=dispatch_id,
        release_sha=release_sha,
        develop_sha=develop_sha,
        run_url=run_url,
    )


def git(workspace: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(workspace), *args], capture_output=True, text=True, check=False
    )


def main(*, workspace: Path, env: dict) -> str:
    result_path = workspace / ".autofix" / "result.json"
    try:
        status = json.loads(result_path.read_text()).get("status", "failed")
    except (OSError, ValueError):
        status = "failed"
    if status != "verified":
        return status

    short_id = env.get("AUTOFIX_SHORT_ID", "")
    dispatch_id = env.get("AUTOFIX_DISPATCH_ID", "")
    base = env.get("AUTOFIX_BASE_BRANCH", "develop")

    changed = git(workspace, "status", "--porcelain", *EXCLUDE_AUTOFIX_DIR).stdout.strip()
    if not changed:
        print("::error::session reported verified but left no diff")
        return "failed"

    summary_path = workspace / ".autofix" / "summary.md"
    summary = summary_path.read_text() if summary_path.is_file() else "(no summary)"
    first_line = next(
        (l.lstrip("# ").strip() for l in summary.splitlines() if l.strip()), "automated fix"
    )

    develop_sha = git(workspace, "rev-parse", "HEAD").stdout.strip()
    branch = branch_name(short_id, dispatch_id)
    bot_login = "sentasity-sentinel[bot]"
    lookup = subprocess.run(
        ["gh", "api", f"/users/{bot_login}", "--jq", ".id"],
        capture_output=True, text=True, check=False,
    )
    if lookup.returncode != 0:
        print(f"::error::gh api /users/{bot_login} failed: {lookup.stderr[:500]}")
        return "failed"
    bot_id = lookup.stdout.strip() or "0"
    author = f"{bot_login} <{bot_id}+{bot_login}@users.noreply.github.com>"

    steps = [
        ("checkout", ["checkout", "-b", branch]),
        ("add", ["add", "--all", *EXCLUDE_AUTOFIX_DIR]),
        ("commit", [
            "-c", f"user.name={bot_login}",
            "-c", f"user.email={bot_id}+{bot_login}@users.noreply.github.com",
            "commit", "--author", author, "-m", f"fix: {first_line} ({short_id})",
        ]),
        ("push", ["push", "origin", branch]),
    ]
    for label, args in steps:
        completed = git(workspace, *args)
        if completed.returncode != 0:
            print(f"::error::git {label} failed: {completed.stderr[:500]}")
            return "failed"

    body = pr_body(
        summary=summary,
        dispatch_id=dispatch_id,
        release_sha=env.get("AUTOFIX_RELEASE_SHA", ""),
        develop_sha=develop_sha,
        run_url=env.get("AUTOFIX_RUN_URL", ""),
    )
    created = subprocess.run(
        [
            "gh", "pr", "create",
            "--repo", env.get("AUTOFIX_TARGET_REPO", ""),
            "--base", base, "--head", branch,
            "--title", pr_title(short_id, first_line),
            "--body", body,
            "--label", "automation",
        ],
        capture_output=True, text=True, check=False, cwd=str(workspace),
    )
    if created.returncode != 0:
        print(f"::error::gh pr create failed: {created.stderr[:500]}")
        return "failed"

    print(f"pr_url={created.stdout.strip()}")
    return "pr_opened"


if __name__ == "__main__":
    final = main(workspace=Path(os.environ["AUTOFIX_WORKSPACE"]), env=dict(os.environ))
    print(final)
    sys.exit(0)
