"""Invariants the public tree must keep.

These guard the open-sourcing work: no deployment-specific values and no
committed real config. The suite reads what git tracks, not what sits on
disk, because an untracked file is exactly what "not published" means here.
"""

import re
import subprocess
from pathlib import Path

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


# Strings that must never reach the public tree. `seer` is another vendor's
# product this project is not positioned against; the Function URL host is a
# live deployment endpoint that should not be advertised. Tasks 1.8 and 1.9
# append further patterns as each clears the tree of what it bans.
BANNED = (
    re.compile(r"\bseer\b", re.IGNORECASE),
    re.compile(r"lambda-url\.[a-z0-9-]+\.on\.aws"),
    re.compile(r"886557787053"),
    re.compile(r"sentasity/product"),
    re.compile(r"#7(87|89|91)\b"),
    re.compile(r"erik", re.IGNORECASE),
    re.compile(r"backend/src/sentasity"),
    re.compile(r"19:[0-9a-f]{32}@thread\.tacv2"),
    # Captured from the live Sentry org and the real GitHub App: every id in
    # the org's own 4511-prefixed family (the organization itself, whose number
    # is also the one inside the DSN hostname, plus its team and its projects),
    # two Sentry account ids, the Teams integration id, and the autofix App id
    # the example config already ships as REPLACE_WITH_APP_ID.
    #
    # None of these carry `\b`, deliberately. An underscore is a word
    # character, so `\b1234\b` never matches `app_1234_id` — the same blind
    # spot that let `\berik\b` through until it was caught in review.
    re.compile(r"4511\d{12}"),
    re.compile(r"4393314|4399821"),
    re.compile(r"390523"),
    re.compile(r"4629139"),
)


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
    hits = [
        f"{name} ({pattern.pattern})"
        for name, body in tracked_text()
        for pattern in BANNED
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
