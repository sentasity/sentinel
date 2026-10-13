"""The issue proxy's operation set, validation, replay keys and text."""

import copy
import dataclasses
import re

import pytest

from receiver.config import ReceiverConfig
from receiver.issues import (
    COMMENT_BODY_LIMIT,
    GRANT_CAPS,
    GRANT_FIELDS,
    ISSUE_BODY_LIMIT,
    ISSUE_TITLE_LIMIT,
    LABEL_LIMIT,
    LABEL_NAME_LIMIT,
    OP_FIELDS,
    OPS,
    OPTIONAL_FIELDS,
    RELATED_PER_RESULT,
    SEARCH_UNAVAILABLE_LINE,
    TRACKING_CAPS,
    TRACKING_FIELDS,
    TRACKING_REPLIES,
    InvalidIssueOp,
    footer,
    grant_lifetime_seconds,
    is_tracking_op,
    landing_warnings,
    match_labels,
    op_key,
    parse_issue_op,
    shape_body,
    shape_title,
    tracking_reply,
)

CFG = ReceiverConfig(
    environments=("prod", "staging"),
    account="123456789012",
    region="us-east-1",
    table_name="sentinel-alerts",
    alarm_email="ops@example.com",
    tenant_id="tenant-123",
    service_url="https://smba.trafficmanager.net/amer/",
    bot_app_id="app-456",
    channels={"prod": "19:prod@thread.tacv2", "staging": "19:staging@thread.tacv2"},
    sentry_org="acme-tools",
    ssm_prefix="/sentinel",
    automation_dsn="",
    target_repo="acme-tools/checkout",
    issues_enabled=True,
    issues_issue_types=("Bug", "Task"),
    issues_fields=("Priority", "Effort"),
    issues_project_number=3,
)

VALID = {
    "file_issue": {
        "tracking_id": "t-1",
        "op": "file_issue",
        "title": "Fix: empty cart total is None",
        "body": "Problem.",
        "type": "Bug",
        "labels": ["backend"],
        "search_unavailable": False,
    },
    "comment": {
        "tracking_id": "t-1",
        "op": "comment",
        "issue_number": 42,
        "relation": "duplicate",
        "body": "Seen again at this release.",
    },
    "reopen": {
        "tracking_id": "t-1",
        "op": "reopen",
        "issue_number": 42,
        "body": "This error came back.",
    },
    "set_fields": {
        "tracking_id": "t-1",
        "op": "set_fields",
        "issue_number": 42,
        "fields": {"Priority": "High", "Effort": "Quick Win"},
    },
    "add_to_project": {"tracking_id": "t-1", "op": "add_to_project", "issue_number": 42},
}

GRANT = {
    "tracking_id": "t-1",
    "short_id": "CHECKOUT-4B2",
    "environment": "staging",
    "release": "79bad4b79fb044dc6386fa690aae2bc3a6ebcc29",
}


def broken(which: str, **changes) -> dict:
    """The valid `which` request with `changes` applied; ... drops the key."""
    body = copy.deepcopy(VALID[which])
    for key, value in changes.items():
        if value is ...:
            body.pop(key)
        else:
            body[key] = value
    return body


@pytest.mark.parametrize("op", OPS)
def test_each_valid_op_parses_to_exactly_what_was_sent(op):
    assert parse_issue_op(copy.deepcopy(VALID[op]), CFG) == VALID[op]


@pytest.mark.parametrize(
    ("body", "rule"),
    [
        (["not", "an", "object"], "JSON object"),
        (broken("comment", tracking_id=7), "tracking_id"),
        (broken("comment", tracking_id=""), "tracking_id"),
        (broken("comment", op="close_issue"), "op must be one of"),
        (broken("comment", op=["comment"]), "op must be one of"),
        (broken("comment", title="smuggled"), "comment takes only"),
        (broken("add_to_project", body="smuggled"), "add_to_project takes only"),
        (broken("file_issue", title="   "), "title must be a non-empty string"),
        (broken("file_issue", title="x" * (ISSUE_TITLE_LIMIT + 1)), "title exceeds"),
        (broken("file_issue", title="Fix: one\nline"), "one line"),
        (broken("file_issue", title="Fix: one\rline"), "one line"),
        (broken("file_issue", body=...), "file_issue needs body"),
        (broken("file_issue", body=5), "body must be a string"),
        (broken("file_issue", body="x" * (ISSUE_BODY_LIMIT + 1)), f"exceeds {ISSUE_BODY_LIMIT}"),
        (broken("comment", body=" \n "), "a comment body must not be empty"),
        (broken("comment", body="x" * (COMMENT_BODY_LIMIT + 1)), f"exceeds {COMMENT_BODY_LIMIT}"),
        (broken("reopen", body=""), "a reopen body must not be empty"),
        (broken("file_issue", type="Epic"), "type must be one of"),
        (broken("file_issue", type=1), "type must be one of"),
        (broken("file_issue", labels="backend"), "labels must be a list"),
        (broken("file_issue", labels=[f"l{i}" for i in range(LABEL_LIMIT + 1)]), "labels must be a list"),
        (broken("file_issue", labels=["x" * (LABEL_NAME_LIMIT + 1)]), "each label"),
        (broken("file_issue", labels=[""]), "each label"),
        (broken("file_issue", labels=[3]), "each label"),
        (broken("file_issue", labels=["backend", "backend"]), "distinct"),
        (broken("file_issue", search_unavailable="yes"), "search_unavailable must be a boolean"),
        (broken("comment", issue_number=0), "issue_number"),
        (broken("comment", issue_number=True), "issue_number"),
        (broken("comment", issue_number="42"), "issue_number"),
        (broken("comment", issue_number=4.2), "issue_number"),
        (broken("comment", issue_number=...), "comment needs issue_number"),
        (broken("comment", relation="same"), "relation"),
        (broken("comment", relation=...), "comment needs relation"),
        (broken("set_fields", fields={}), "at least one entry"),
        (broken("set_fields", fields=["Priority"]), "at least one entry"),
        (broken("set_fields", fields={"Status": "Done"}), "only the tracking block's fields"),
        (broken("set_fields", fields={"Priority": "  "}), "non-empty option name"),
        (broken("set_fields", fields={"Priority": 1}), "non-empty option name"),
    ],
)
def test_each_rule_rejects_a_request_that_breaks_only_it(body, rule):
    with pytest.raises(InvalidIssueOp, match=re.escape(rule)):
        parse_issue_op(body, CFG)


def test_add_to_project_needs_a_configured_board():
    cfg = dataclasses.replace(CFG, issues_project_number=0)

    with pytest.raises(InvalidIssueOp, match="needs a project board"):
        parse_issue_op(copy.deepcopy(VALID["add_to_project"]), cfg)


def test_the_boundary_values_are_accepted():
    body = broken(
        "file_issue",
        title="x" * ISSUE_TITLE_LIMIT,
        body="x" * ISSUE_BODY_LIMIT,
        labels=[f"l{i}" for i in range(LABEL_LIMIT - 1)] + ["x" * LABEL_NAME_LIMIT],
    )

    assert parse_issue_op(body, CFG)["title"] == "x" * ISSUE_TITLE_LIMIT


def test_an_empty_create_body_is_accepted():
    """The receiver's own lines still make a body; only a comment must say something."""
    assert parse_issue_op(broken("file_issue", body=""), CFG)["body"] == ""


@pytest.mark.parametrize("missing", sorted(OPTIONAL_FIELDS))
def test_an_optional_field_left_out_or_null_is_absent_from_the_op(missing):
    left_out = parse_issue_op(broken("file_issue", **{missing: ...}), CFG)
    nulled = parse_issue_op(broken("file_issue", **{missing: None}), CFG)

    assert missing not in left_out
    assert left_out == nulled


def test_a_required_field_sent_as_null_is_missing():
    with pytest.raises(InvalidIssueOp, match="reopen needs body"):
        parse_issue_op(broken("reopen", body=None), CFG)


def test_no_message_echoes_a_value_the_request_carried():
    """The session reads text strangers wrote; a reason must not carry it back."""
    marker = "INJECTED-VALUE"
    for body in (
        broken("file_issue", type=marker),
        broken("comment", relation=marker),
        broken("set_fields", fields={marker: "High"}),
        broken("comment", **{marker: 1}),
    ):
        with pytest.raises(InvalidIssueOp) as refused:
            parse_issue_op(body, CFG)
        assert marker not in str(refused.value)


def test_the_op_key_ignores_key_order_and_whitespace():
    reordered = dict(reversed(list(VALID["comment"].items())))

    assert op_key(VALID["comment"]) == op_key(reordered)
    assert re.fullmatch(r"[0-9a-f]{64}", op_key(VALID["comment"]))


def test_the_op_key_changes_with_any_field():
    assert op_key(VALID["comment"]) != op_key(broken("comment", body="Seen again."))
    assert op_key(VALID["comment"]) != op_key(broken("comment", tracking_id="t-2"))


def test_only_a_create_a_reopen_or_a_duplicate_comment_tracks_the_alert():
    assert is_tracking_op(VALID["file_issue"])
    assert is_tracking_op(VALID["reopen"])
    assert is_tracking_op(VALID["comment"])
    assert not is_tracking_op(broken("comment", relation="related"))
    assert not is_tracking_op(VALID["set_fields"])
    assert not is_tracking_op(VALID["add_to_project"])


def test_the_footer_names_the_grants_sentry_issue():
    assert footer(GRANT) == "Sentinel · Sentry CHECKOUT-4B2 · staging · release 79bad4b"


def test_shape_body_redacts_and_ends_with_the_footer():
    body, redactions = shape_body("Reported by ops@example.com from 10.0.0.1.", GRANT)

    assert redactions == 2
    assert "ops@example.com" not in body
    assert "[redacted: email]" in body
    assert body.endswith("\n\nSentinel · Sentry CHECKOUT-4B2 · staging · release 79bad4b")


def test_shape_title_redacts_and_adds_nothing():
    assert shape_title("Fix: login fails for ops@example.com") == (
        "Fix: login fails for [redacted: email]", 1,
    )
    assert shape_title("Fix: empty cart total is None") == ("Fix: empty cart total is None", 0)


def test_shape_body_puts_the_search_line_first_only_when_flagged():
    flagged, _ = shape_body("Problem.", GRANT, search_unavailable=True)
    plain, _ = shape_body("Problem.", GRANT)

    assert flagged.startswith(SEARCH_UNAVAILABLE_LINE + "\n\nProblem.")
    assert SEARCH_UNAVAILABLE_LINE not in plain
    assert plain.startswith("Problem.")


def test_an_empty_body_still_carries_the_footer():
    body, _ = shape_body("", GRANT)

    assert body == footer(GRANT)


def test_a_footer_the_session_wrote_is_dropped_so_the_post_ends_in_one():
    """A session that read an earlier Sentinel issue copied its footer into
    a comment, and the receiver's own footer then followed it."""
    body, _ = shape_body(
        "Re-investigated at this release.\n\nSentinel · Sentry CHECKOUT-4B2 · staging · release 79bad4b\n",
        GRANT,
    )

    assert body == "Re-investigated at this release.\n\n" + footer(GRANT)


def test_a_footer_shaped_line_naming_another_issue_never_survives():
    """Which Sentry issue a post concerns is the receiver's to say, and the
    footer is the line a later sweep searches for, so a session-written one
    anywhere in the text would point that sweep at the wrong issue."""
    body, _ = shape_body(
        "Problem.\nSentinel · Sentry OTHER-1 · prod · release abcdef0\nMore detail.", GRANT
    )

    assert "OTHER-1" not in body
    assert "More detail." in body
    assert body.count("Sentinel · Sentry") == 1
    assert body.endswith(footer(GRANT))


def test_the_search_line_is_the_exact_shared_string():
    """The drain skill searches for this string byte for byte."""
    assert SEARCH_UNAVAILABLE_LINE == (
        "Sentinel could not search for duplicate issues when it filed this one; "
        "check for an existing issue before working it."
    )


def test_the_tracking_caps_are_the_limits_the_validation_enforces():
    assert TRACKING_CAPS == {
        "related_per_result": RELATED_PER_RESULT,
        "title_max_chars": ISSUE_TITLE_LIMIT,
        "body_max_chars": ISSUE_BODY_LIMIT,
        "comment_max_chars": COMMENT_BODY_LIMIT,
    }
    assert RELATED_PER_RESULT == GRANT_CAPS["comment"] - 1 == 5


def test_every_op_has_its_fields_and_a_cap():
    assert set(OP_FIELDS) == set(OPS)
    assert set(GRANT_CAPS) == set(OPS)
    assert OPTIONAL_FIELDS <= set(OP_FIELDS["file_issue"])


def test_the_tracking_payload_keys_are_pinned():
    """The investigator prompt reads these names verbatim."""
    assert TRACKING_FIELDS == (
        "repo", "issues_url", "issue_types", "fields", "project", "caps", "grants",
    )
    assert GRANT_FIELDS == (
        "issue_id", "short_id", "tracking_id", "tracking_token", "expires_at",
    )


def test_a_granted_result_waits_out_its_fix_phase_before_its_window_starts():
    assert grant_lifetime_seconds("always") == 1800
    assert grant_lifetime_seconds("unless_pr") == 3600 + 1800


@pytest.mark.parametrize(
    ("op", "state", "flag", "expected"),
    [
        (VALID["file_issue"], "", False, "📌 Tracked in GitHub: https://x/1"),
        (
            VALID["file_issue"],
            "",
            True,
            "📌 Tracked in GitHub: https://x/1\n\n"
            "⚠️ The duplicate search was unavailable, so this issue may duplicate an existing one.",
        ),
        (
            VALID["comment"],
            "open",
            False,
            "📌 Already tracked in GitHub; this occurrence was added to https://x/1",
        ),
        (
            VALID["comment"],
            "closed",
            False,
            "📌 Already tracked in GitHub by a closed issue; this occurrence was added to https://x/1",
        ),
        (
            VALID["reopen"],
            "closed",
            False,
            "📌 Reopened in GitHub, since this error came back after the issue was closed: https://x/1",
        ),
        (broken("comment", relation="related"), "open", False, ""),
        (VALID["set_fields"], "", False, ""),
        (VALID["add_to_project"], "", False, ""),
    ],
)
def test_each_tracking_outcome_has_its_one_thread_line(op, state, flag, expected):
    assert tracking_reply(op, url="https://x/1", issue_state=state, search_unavailable=flag) == expected


def test_a_reply_with_no_usable_link_says_so():
    assert tracking_reply(VALID["file_issue"], url="") == "📌 Tracked in GitHub: (link unavailable)"


def test_every_reply_string_is_pinned():
    assert set(TRACKING_REPLIES) == {
        "file_issue", "duplicate_open", "duplicate_closed", "reopen", "search_unavailable",
    }


def test_labels_match_ignoring_case_and_use_the_repositorys_spelling():
    matched, unknown = match_labels(["backend", "FRONTEND", "nope"], ["Backend", "frontend"])

    assert matched == ["Backend", "frontend"]
    assert unknown == ["nope"]


def test_two_spellings_of_one_label_are_sent_once():
    assert match_labels(["backend", "Backend"], ["Backend"]) == (["Backend"], [])


def test_nothing_that_landed_as_sent_is_warned_about():
    issue = {"labels": [{"name": "Backend"}], "type": {"name": "Bug"}}

    assert landing_warnings(issue, ["Backend"], "Bug") == []


def test_a_label_or_type_that_did_not_land_is_warned_about():
    """GitHub drops a label quietly for a caller without push access, so
    the response is checked rather than trusted."""
    issue = {"labels": [], "type": None}

    assert landing_warnings(issue, ["Backend"], "Bug") == [
        "label Backend did not land on the issue",
        "type Bug did not land on the issue",
    ]


def test_no_type_sent_means_no_type_checked():
    assert landing_warnings({"labels": [], "type": None}, [], "") == []
