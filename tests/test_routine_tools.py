"""The routine's GitHub tool policy must stay complete and agree with the prompt.

`prompts/routine-tools.yaml` is the canonical copy of the tool settings both
routines carry: the two issue reads the session may use, and every other tool
of the platform's built-in GitHub server, withheld. The routines hold their own
copy, applied by hand, so these checks are what keep the file worth applying.
"""

import inspect
import re
from pathlib import Path

import yaml

from receiver.github_app import GitHubAppClient
from receiver.issues import OPS

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

PREFIX = "mcp__github__"

READ_ALLOWLIST = ["search_issues", "issue_read"]

# Write tools a routine session is known to carry, or that the issue
# conventions a session reads point a person at. A careless edit to the list
# must not be able to drop one of these.
WRITE_FLOOR = (
    "add_issue_comment",
    "create_pull_request",
    "enable_pr_auto_merge",
    "issue_write",
    "merge_pull_request",
    "projects_write",
    "push_files",
)

# Reads that would let a session check the names it drafts with. The receiver
# validates every type, label, and field name instead, so these stay withheld,
# and two of them reach GitHub only through GraphQL besides.
NAME_CHECKING_READS = ("get_label", "list_issue_fields", "list_issue_types")

# Tools a probe of the real routine listed beyond the two reads, after the list
# was applied. Each one stays withheld, so a regenerated list cannot drop it.
SEEN_BY_PROBE = ("disable_pr_auto_merge", "get_check_run")


def policy() -> dict:
    return yaml.safe_load((PROMPTS / "routine-tools.yaml").read_text())


def denied() -> list[str]:
    return policy()["disallowed_tools"]


def denied_unprefixed() -> set[str]:
    return {name.removeprefix(PREFIX) for name in denied()}


def github_access() -> str:
    """The investigator prompt's GitHub-access paragraph."""
    body = (PROMPTS / "investigator.md").read_text()
    return body[body.index("GitHub access.") : body.index("Input:")]


def test_the_policy_names_the_built_in_server_and_the_two_reads():
    assert policy()["prefix"] == PREFIX
    assert policy()["read_allowlist"] == READ_ALLOWLIST


def test_every_denied_tool_carries_the_prefix():
    for name in denied():
        assert name.startswith(PREFIX), f"{name} is not a built-in GitHub tool name"


def test_the_deny_list_is_sorted_with_no_duplicates():
    names = denied()

    assert names == sorted(names), "keep disallowed_tools sorted"
    assert len(names) == len(set(names)), "disallowed_tools names a tool twice"


def test_the_deny_list_never_withholds_an_allowed_read():
    allowed = {PREFIX + name for name in policy()["read_allowlist"]}

    assert not allowed & set(denied())


def test_the_deny_list_keeps_the_write_floor_and_the_name_checking_reads():
    missing = (set(WRITE_FLOOR) | set(NAME_CHECKING_READS) | set(SEEN_BY_PROBE)) - denied_unprefixed()

    assert not missing, f"disallowed_tools dropped: {sorted(missing)}"


def test_every_allowed_read_is_named_in_the_github_access_paragraph():
    body = github_access()

    for name in policy()["read_allowlist"]:
        assert f"`{name}`" in body, f"the GitHub-access paragraph never names {name}"


def test_the_investigator_prompt_never_names_a_withheld_tool():
    """A prompt that tells the session to call a withheld tool either fails
    at the call or, wherever the list does not apply, licenses the very
    write the list exists to prevent."""
    body = (PROMPTS / "investigator.md").read_text()
    backticked = {name.removeprefix(PREFIX) for name in re.findall(r"`([^`\s]+)`", body)}
    named = backticked & denied_unprefixed()

    assert not named, f"the investigator prompt names withheld tools: {sorted(named)}"


def test_no_receiver_operation_or_client_method_shares_a_name_with_a_withheld_tool():
    """The prompt names every receiver operation. A withheld tool spelled the
    same way would read to the session as one it may use, and the session
    could not tell which of the two the prompt meant."""
    methods = {
        name
        for name, _ in inspect.getmembers(GitHubAppClient, inspect.isfunction)
        if not name.startswith("_")
    }
    clashes = (methods | set(OPS)) & denied_unprefixed()

    assert not clashes, f"these share a name with a withheld tool: {sorted(clashes)}"
