"""Invariants the public tree must keep.

These guard the open-sourcing work: no deployment-specific values and no
committed real config. The suite reads what git tracks, not what sits on
disk, because an untracked file is exactly what "not published" means here.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def tracked(pattern: str) -> list[str]:
    """Repo-relative paths git tracks under `pattern`."""
    out = subprocess.run(
        ["git", "ls-files", pattern],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def test_only_example_config_files_are_tracked():
    """The real deployment config is local-only; the repo ships templates."""
    for path in tracked("config/*"):
        assert path.endswith(".example"), f"{path} is a real config, not a template"


def test_the_example_config_names_no_specific_deployment():
    body = (REPO / "config" / "receiver.yaml.example").read_text()

    assert "sentasity" not in body.lower()


# Strings that must never reach the public tree, in two tiers.
#
# The patterns below are generic shapes — a Lambda Function URL host, a Teams
# channel id — that identify no particular deployment, so they can be spelled
# out here. The literals that would identify one (account and organization
# ids, private repository paths, people) are exactly what this repository must
# not publish, and a tracked ban list would publish them itself, as an index
# of everything it protects. Those live in an untracked local file instead:
# config/banned.local.txt, one Python regex per line, `#` comments (a pattern
# that must start with a literal # is written `\#`), inline flags like `(?i)`
# for case-insensitivity. CI materializes the same file from an Actions
# secret; see local_banned() below for how its absence degrades.
BANNED = (
    re.compile(r"lambda-url\.[a-z0-9-]+\.on\.aws"),
    re.compile(r"19:[0-9a-f]{32}@thread\.tacv2"),
)

BANNED_LOCAL_FILE = REPO / "config" / "banned.local.txt"


def local_banned() -> list[re.Pattern]:
    """Patterns from the untracked local ban list, if it exists.

    A fork or a fresh clone has no such file and runs on the generic patterns
    alone; that is by design, since the file's contents are the one thing this
    repository will not hand them. The environments that do hold the list set
    SENTINEL_REQUIRE_BANNED_LOCAL so that losing it fails loudly instead of
    quietly narrowing the scan (see the test below).
    """
    if not BANNED_LOCAL_FILE.is_file():
        return []
    patterns = []
    for line in BANNED_LOCAL_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(re.compile(line))
    return patterns


def test_the_local_ban_list_is_present_where_required():
    if not os.environ.get("SENTINEL_REQUIRE_BANNED_LOCAL"):
        pytest.skip("this environment does not hold the local ban list")

    assert local_banned(), (
        f"SENTINEL_REQUIRE_BANNED_LOCAL is set but {BANNED_LOCAL_FILE} is "
        "missing or empty; in CI that means the secret backing it is gone"
    )


def test_the_local_ban_list_is_never_tracked():
    """Tracking it would publish the index the split exists to withhold."""
    assert not tracked("config/banned.local.txt")


def tracked_text() -> list[tuple[str, str]]:
    """Every tracked file that decodes as text, as (path, contents).

    This file is skipped: it necessarily spells out what it bans.
    """
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    files = []
    for name in listing.split("\0"):
        if not name or name == "tests/test_public_tree.py":
            continue
        try:
            files.append((name, (REPO / name).read_text()))
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
    return files


def test_no_tracked_file_carries_a_banned_string():
    # A local pattern's source is never printed: this failure message lands in
    # a public CI log, and the local tier holds exactly the literals that must
    # not be published. The file name is enough to find the hit; run the suite
    # locally to see which pattern fired.
    labelled = [(pattern, pattern.pattern) for pattern in BANNED] + [
        (pattern, f"a pattern from {BANNED_LOCAL_FILE.name}")
        for pattern in local_banned()
    ]
    hits = [
        f"{name} ({label})"
        for name, body in tracked_text()
        for pattern, label in labelled
        if pattern.search(body)
    ]

    assert not hits, "banned strings in the public tree: " + "; ".join(hits)


def test_the_scan_actually_reads_files():
    """A silent failure in `git ls-files` would make the scan vacuous."""
    names = [name for name, _ in tracked_text()]

    assert "README.md" in names
    assert len(names) > 20


OSS_FILES = ("LICENSE", "NOTICE", "CONTRIBUTING.md", "SECURITY.md")


def test_the_standard_open_source_files_are_present():
    for name in OSS_FILES:
        assert (REPO / name).is_file(), f"{name} is missing"


def test_the_license_is_apache_2():
    body = (REPO / "LICENSE").read_text()

    assert "Apache License" in body
    assert "Version 2.0, January 2004" in body


def test_the_readme_names_the_claude_mechanism_without_cost_framing():
    """The mechanism is worth stating; the comparison to metered billing is
    not, and it reads as positioning rather than description."""
    body = (REPO / "README.md").read_text()

    for phrase in ("metered API credits", "Claude subscription"):
        assert phrase not in body, f"README still carries the cost framing: {phrase}"

    assert "Claude Code cloud routines" in body


def test_the_readme_points_at_the_license():
    assert "[LICENSE](LICENSE)" in (REPO / "README.md").read_text()
