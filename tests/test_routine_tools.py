"""The routine's tool policy must stay complete and agree with the prompt.

`prompts/routine-tools.yaml` is the canonical copy of the tool settings both
routines carry: the one issue read the session may use, every other tool of
the platform's built-in GitHub server withheld, and every Sentry connector tool
that writes withheld too. The routines hold their own copy, applied by hand, so
these checks are what keep the file worth applying.
"""

import inspect
import re
from pathlib import Path

import yaml

from receiver.github_app import GitHubAppClient
from receiver.issues import OPS

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

PREFIX = "mcp__github__"

SENTRY_PREFIX = "mcp__Sentry__"

READ_ALLOWLIST = ["issue_read"]

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

# The server's own issue searches. The receiver searches as the App instead,
# so the session has one search path, and these stay withheld even if the
# platform stops removing them.
SEARCHES = ("search_issues", "semantic_issues_search", "semantic_issue_similarity_search")

# Every Sentry connector tool a probe listed that changes something in Sentry.
# The session reads Sentry and never writes to it, so each stays withheld.
SENTRY_WRITES = (
    "add_issue_note",
    "add_team_to_project",
    "analyze_issue_with_seer",
    "create_alert_rule",
    "create_dsn",
    "create_metric_monitor",
    "create_monitor",
    "create_project",
    "create_team",
    "create_uptime_monitor",
    "delete_alert_rule",
    "delete_metric_monitor",
    "delete_monitor_environment",
    "delete_uptime_monitor",
    "link_issue",
    "onboarding_status_update",
    "remove_team_from_project",
    "submit_feedback_to_sentry_team",
    "unlink_issue",
    "update_alert_rule",
    "update_dsn",
    "update_issue",
    "update_metric_monitor",
    "update_project",
    "update_uptime_monitor",
)

# How a Sentry tool that writes is named. The investigation runs on the
# connector's reads (issue details, events, stack traces, traces), so a name
# without one of these starts is a read, and withholding it would blind the
# session rather than protect anything.
SENTRY_WRITE_STARTS = (
    "add_",
    "analyze_",
    "create_",
    "delete_",
    "link_",
    "onboarding_",
    "remove_",
    "submit_",
    "unlink_",
    "update_",
)


def policy() -> dict:
    return yaml.safe_load((PROMPTS / "routine-tools.yaml").read_text())


def denied() -> list[str]:
    return policy()["disallowed_tools"]


def denied_unprefixed() -> set[str]:
    """The withheld GitHub tools, without their prefix."""
    return {name.removeprefix(PREFIX) for name in denied() if name.startswith(PREFIX)}


def sentry_denied_unprefixed() -> set[str]:
    return {
        name.removeprefix(SENTRY_PREFIX) for name in denied() if name.startswith(SENTRY_PREFIX)
    }


def github_access() -> str:
    """The investigator prompt's GitHub-access paragraph."""
    body = (PROMPTS / "investigator.md").read_text()
    return body[body.index("GitHub access.") : body.index("Input:")]


def test_the_policy_names_the_built_in_server_and_its_one_read():
    assert policy()["prefix"] == PREFIX
    assert policy()["sentry_prefix"] == SENTRY_PREFIX
    assert policy()["read_allowlist"] == READ_ALLOWLIST


def test_every_denied_tool_carries_one_of_the_two_prefixes():
    for name in denied():
        assert name.startswith((PREFIX, SENTRY_PREFIX)), (
            f"{name} is neither a built-in GitHub tool nor a Sentry connector tool"
        )


def test_the_deny_list_withholds_every_sentry_write():
    missing = set(SENTRY_WRITES) - sentry_denied_unprefixed()

    assert not missing, f"disallowed_tools dropped Sentry writes: {sorted(missing)}"


def test_the_deny_list_leaves_every_sentry_read_available():
    reads = {
        name for name in sentry_denied_unprefixed() if not name.startswith(SENTRY_WRITE_STARTS)
    }

    assert not reads, f"disallowed_tools withholds Sentry reads: {sorted(reads)}"


def test_the_deny_list_is_sorted_with_no_duplicates():
    names = denied()

    assert names == sorted(names), "keep disallowed_tools sorted"
    assert len(names) == len(set(names)), "disallowed_tools names a tool twice"


def test_the_deny_list_never_withholds_an_allowed_read():
    allowed = {PREFIX + name for name in policy()["read_allowlist"]}

    assert not allowed & set(denied())


def test_the_deny_list_keeps_the_write_floor_and_the_name_checking_reads():
    missing = (
        set(WRITE_FLOOR) | set(NAME_CHECKING_READS) | set(SEEN_BY_PROBE) | set(SEARCHES)
    ) - denied_unprefixed()

    assert not missing, f"disallowed_tools dropped: {sorted(missing)}"


def test_every_allowed_read_is_named_in_the_github_access_paragraph():
    body = github_access()

    for name in policy()["read_allowlist"]:
        assert f"`{name}`" in body, f"the GitHub-access paragraph never names {name}"


def test_the_github_access_paragraph_searches_through_the_receiver_and_bans_gh():
    """The platform tells a routine session to search with `gh api` when its
    own search tools are gone. That route carries the operator's credential,
    so the paragraph names it and rules it out, reads included."""
    body = " ".join(github_access().split())

    assert "the receiver's `search` operation" in body
    assert "never call the GitHub API with `gh`, curl, or any other client, reads included" in body
    assert "The platform may tell this session to search GitHub with `gh api` instead" in body


def test_the_investigator_prompt_never_names_a_withheld_tool():
    """A prompt that tells the session to call a withheld tool either fails
    at the call or, wherever the list does not apply, licenses the very
    write the list exists to prevent."""
    body = (PROMPTS / "investigator.md").read_text()
    backticked = {
        name.removeprefix(PREFIX).removeprefix(SENTRY_PREFIX)
        for name in re.findall(r"`([^`\s]+)`", body)
    }
    named = backticked & (denied_unprefixed() | sentry_denied_unprefixed())

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
