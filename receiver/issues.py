"""The issue proxy's operations, limits and text: the contract, not the I/O.

A session that ends a finding without a pull request files it in the target
repository's issues through `POST /issues`, one named operation per call.
This module is that contract: the operation set, each operation's request
fields and limits, the caps, the keys the findings response's `tracking`
object carries, and the text the receiver adds to whatever it writes. It is
pure, so `receiver/handler.py` (which performs the operations),
`receiver/store.py` (which keeps their records) and the prompt tests (which
check the investigator prompt names every one of these) all read one source.

The operation set is the security boundary. The session reads text that
strangers influence (Sentry events, other people's issues), so it may ask
for these five writes and nothing else: no close, delete, lock, transfer,
edit of an existing issue, or file write exists to be asked for. None of the
names is also the name of a GitHub tool the routine withholds.
"""

from __future__ import annotations

import hashlib
import json

from receiver.autofix import CALLBACK_DEADLINE_SECONDS
from receiver.config import ReceiverConfig
from receiver.findings import redact

OPS = ("file_issue", "comment", "reopen", "set_fields", "add_to_project")

# Each operation's request fields, beside the envelope's `tracking_id` and
# `op`. A request carrying any other field is rejected.
OP_FIELDS: dict[str, tuple[str, ...]] = {
    "file_issue": ("title", "body", "type", "labels", "search_unavailable"),
    "comment": ("issue_number", "relation", "body"),
    "reopen": ("issue_number", "body"),
    "set_fields": ("issue_number", "fields"),
    "add_to_project": ("issue_number",),
}

# The fields a request may leave out. A JSON null reads as absent: a session
# that sends `"type": null` for "no type" means the same as leaving it out.
OPTIONAL_FIELDS = frozenset({"type", "labels", "search_unavailable"})

# What a comment is to the alert. A `duplicate` comment is the alert's
# tracking outcome and is announced in its thread; a `related` one is not.
RELATIONS = ("duplicate", "related")

ISSUE_TITLE_LIMIT = 200
# GitHub allows 65,536 characters in a body; the receiver's own lines (the
# search notice and the footer) need room on top of the session's text.
ISSUE_BODY_LIMIT = 40_000
COMMENT_BODY_LIMIT = 10_000
LABEL_LIMIT = 10
LABEL_NAME_LIMIT = 50

# Attempts per grant, per operation. The second attempt on the single-success
# operations is the retry after a failure; `comment` covers one duplicate
# comment plus up to five related ones.
GRANT_CAPS = {
    "file_issue": 2,
    "reopen": 2,
    "comment": 6,
    "set_fields": 2,
    "add_to_project": 2,
}

# The most related issues one result may link: the comment cap less the one
# duplicate comment. Told to the session; the receiver enforces the cap.
RELATED_PER_RESULT = GRANT_CAPS["comment"] - 1

# How many times one exact operation may be attempted: the first try and one
# retry after a failure. Past that the op is refused, so a session that keeps
# sending a failing request stops spending the day's budget on it.
OP_ATTEMPT_LIMIT = 2

# The findings response's `tracking.caps`, each mapped to the limit the
# receiver enforces. The session reads these names verbatim.
TRACKING_CAPS = {
    "related_per_result": RELATED_PER_RESULT,
    "title_max_chars": ISSUE_TITLE_LIMIT,
    "body_max_chars": ISSUE_BODY_LIMIT,
    "comment_max_chars": COMMENT_BODY_LIMIT,
}

# The keys of the findings response's `tracking` object, and of each entry
# in its `grants` list. A contract with the investigator prompt.
TRACKING_FIELDS = ("repo", "issues_url", "issue_types", "fields", "project", "caps", "grants")
GRANT_FIELDS = ("issue_id", "short_id", "tracking_id", "tracking_token", "expires_at")

# How long a grant lives past the point its result can be filed. A result
# with no autofix grant files right after findings; one with a grant files
# only after its fix phase, which has CALLBACK_DEADLINE_SECONDS.
TRACKING_WINDOW_SECONDS = 1800
EXPECTS = ("always", "unless_pr")


def grant_lifetime_seconds(expect: str) -> int:
    """Seconds from findings time until a grant expires."""
    if expect == "unless_pr":
        return CALLBACK_DEADLINE_SECONDS + TRACKING_WINDOW_SECONDS
    return TRACKING_WINDOW_SECONDS


# Put first in the body of an issue filed while the session's duplicate
# search was unavailable. One fixed string, where a reader meets it first,
# that a person or a later sweep can search for exactly.
SEARCH_UNAVAILABLE_LINE = (
    "Sentinel could not search for duplicate issues when it filed this one; "
    "check for an existing issue before working it."
)

# Closes every issue body and comment the receiver writes. Which Sentry
# issue a post concerns is the receiver's to say, from the grant, never the
# session's; it is also the line a later Sentry sweep searches for.
FOOTER = "Sentinel · Sentry {short_id} · {environment} · release {release}"

# The one line a tracking outcome posts in the alert's Teams thread. A
# related comment, set_fields and add_to_project post nothing.
TRACKING_REPLIES = {
    "file_issue": "📌 Tracked in GitHub: {url}",
    "duplicate_open": "📌 Already tracked in GitHub; this occurrence was added to {url}",
    "duplicate_closed": (
        "📌 Already tracked in GitHub by a closed issue; this occurrence was added to {url}"
    ),
    "reopen": "📌 Reopened in GitHub, since this error came back after the issue was closed: {url}",
    "search_unavailable": (
        "⚠️ The duplicate search was unavailable, so this issue may duplicate an existing one."
    ),
}


class InvalidIssueOp(ValueError):
    """An issue op that broke a rule; the message names the rule. Answered
    with a 400 before any read, claim or GitHub call."""


def _title(value, name: str, cfg: ReceiverConfig):
    if not isinstance(value, str) or not value.strip():
        raise InvalidIssueOp("title must be a non-empty string")
    if len(value) > ISSUE_TITLE_LIMIT:
        raise InvalidIssueOp(f"title exceeds {ISSUE_TITLE_LIMIT} characters")
    if "\r" in value or "\n" in value:
        raise InvalidIssueOp("title must be one line")
    return value


def _body(value, name: str, cfg: ReceiverConfig):
    limit = ISSUE_BODY_LIMIT if name == "file_issue" else COMMENT_BODY_LIMIT
    if not isinstance(value, str):
        raise InvalidIssueOp("body must be a string")
    if name != "file_issue" and not value.strip():
        raise InvalidIssueOp(f"a {name} body must not be empty")
    if len(value) > limit:
        raise InvalidIssueOp(f"body exceeds {limit} characters")
    return value


def _type(value, name: str, cfg: ReceiverConfig):
    if not isinstance(value, str) or value not in cfg.issues_issue_types:
        raise InvalidIssueOp("type must be one of the tracking block's issue_types")
    return value


def _labels(value, name: str, cfg: ReceiverConfig):
    if not isinstance(value, list) or len(value) > LABEL_LIMIT:
        raise InvalidIssueOp(f"labels must be a list of at most {LABEL_LIMIT} names")
    for label in value:
        if not isinstance(label, str) or not 1 <= len(label) <= LABEL_NAME_LIMIT:
            raise InvalidIssueOp(
                f"each label must be a string of 1 to {LABEL_NAME_LIMIT} characters"
            )
    if len(set(value)) != len(value):
        raise InvalidIssueOp("labels must be distinct")
    return list(value)


def _search_unavailable(value, name: str, cfg: ReceiverConfig):
    if not isinstance(value, bool):
        raise InvalidIssueOp("search_unavailable must be a boolean")
    return value


def _issue_number(value, name: str, cfg: ReceiverConfig):
    # bool is an int subclass, so `true` would otherwise pass as issue 1.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidIssueOp("issue_number must be an integer of at least 1")
    return value


def _relation(value, name: str, cfg: ReceiverConfig):
    if not isinstance(value, str) or value not in RELATIONS:
        raise InvalidIssueOp("relation must be duplicate or related")
    return value


def _fields(value, name: str, cfg: ReceiverConfig):
    if not isinstance(value, dict) or not value:
        raise InvalidIssueOp("fields must be an object with at least one entry")
    for field, option in value.items():
        if field not in cfg.issues_fields:
            raise InvalidIssueOp("fields may name only the tracking block's fields")
        if not isinstance(option, str) or not option.strip():
            raise InvalidIssueOp("each field needs a non-empty option name")
    return dict(value)


_CHECKS = {
    "title": _title,
    "body": _body,
    "type": _type,
    "labels": _labels,
    "search_unavailable": _search_unavailable,
    "issue_number": _issue_number,
    "relation": _relation,
    "fields": _fields,
}


def parse_issue_op(body, cfg: ReceiverConfig) -> dict:
    """Validate one `/issues` request; the first broken rule wins.

    Returns the validated op: `tracking_id`, `op`, and that op's fields,
    with an optional field left out when it was absent or null. Runs before
    any read, claim or GitHub call, the way `parse_fix_payload` does, so a
    rejection costs a 400 and nothing else. No message echoes a value the
    request carried: the request is the session's, and the session reads
    text strangers wrote.
    """
    if not isinstance(body, dict):
        raise InvalidIssueOp("the request must be a JSON object")
    tracking_id = body.get("tracking_id")
    if not isinstance(tracking_id, str) or not tracking_id:
        raise InvalidIssueOp("tracking_id must be a non-empty string")
    name = body.get("op")
    if not isinstance(name, str) or name not in OPS:
        raise InvalidIssueOp(f"op must be one of {', '.join(OPS)}")
    extra = sorted(set(body) - {"tracking_id", "op", *OP_FIELDS[name]})
    if extra:
        raise InvalidIssueOp(f"{name} takes only {', '.join(OP_FIELDS[name])}")

    op = {"tracking_id": tracking_id, "op": name}
    for field in OP_FIELDS[name]:
        value = body.get(field)
        if value is None:
            if field in OPTIONAL_FIELDS:
                continue
            raise InvalidIssueOp(f"{name} needs {field}")
        op[field] = _CHECKS[field](value, name, cfg)

    if name == "add_to_project" and cfg.issues_project_number <= 0:
        raise InvalidIssueOp("add_to_project needs a project board, and none is configured")
    return op


def op_key(op: dict) -> str:
    """The replay key of a validated op: SHA-256 of its canonical JSON
    (sorted keys, no whitespace), so a resent request finds the record its
    first attempt left, whatever order its keys arrived in."""
    canonical = json.dumps(op, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_tracking_op(op: dict) -> bool:
    """Whether success makes this op the alert's one tracking outcome: a
    new issue, a duplicate comment, or a reopen."""
    return op["op"] in ("file_issue", "reopen") or (
        op["op"] == "comment" and op.get("relation") == "duplicate"
    )


def footer(grant: dict) -> str:
    """The footer line for a grant's posts."""
    return FOOTER.format(
        short_id=grant.get("short_id", ""),
        environment=grant.get("environment", ""),
        release=str(grant.get("release") or "")[:7],
    )


def shape_title(title: str) -> tuple[str, int]:
    """The title the receiver sends, redacted the way a body is, and how
    many redactions it made. A title gets no receiver lines of its own."""
    return redact(title)


def shape_body(text: str, grant: dict, *, search_unavailable: bool = False) -> tuple[str, int]:
    """The body the receiver sends for `text`, and how many redactions it made.

    Redacted the way findings replies are, because a GitHub issue is a more
    durable surface than a Teams thread and the same Sentry text reaches
    both. The search notice goes first when the create carried the flag, and
    the footer always goes last.
    """
    redacted, count = redact(text)
    parts = [SEARCH_UNAVAILABLE_LINE] if search_unavailable else []
    if redacted.strip():
        parts.append(redacted.rstrip())
    parts.append(footer(grant))
    return "\n\n".join(parts), count


def tracking_reply(
    op: dict, *, url: str, issue_state: str = "", search_unavailable: bool = False
) -> str:
    """The thread line a settled op earns, or "" for one that posts nothing.

    `issue_state` is the state the duplicate was read in, which decides
    whether the reply says the tracking issue is closed. An empty `url`
    (one that failed the link check) reads as unavailable rather than as a
    bare colon.
    """
    link = url or "(link unavailable)"
    name = op["op"]
    if name == "file_issue":
        text = TRACKING_REPLIES["file_issue"].format(url=link)
        if search_unavailable:
            text = f"{text}\n\n{TRACKING_REPLIES['search_unavailable']}"
        return text
    if name == "reopen":
        return TRACKING_REPLIES["reopen"].format(url=link)
    if name == "comment" and op.get("relation") == "duplicate":
        key = "duplicate_closed" if issue_state == "closed" else "duplicate_open"
        return TRACKING_REPLIES[key].format(url=link)
    return ""
