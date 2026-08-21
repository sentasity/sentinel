"""Validating, rendering, and redacting the findings a session reports."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from receiver.cards import SCHEMA, VERSION, escape_markdown

LOG = logging.getLogger(__name__)

# Written against the one component that emits PII. Exactly one Sentry init in
# the product repo sets send_default_pii=True: the trigger Lambda
# (src/example_app/handlers/trigger/main.py:93, backend-api
# project). Every other init sets it False. So these patterns target that known
# surface (request headers, cookies, client IP, request body) where they can be
# specific enough to test, while the pass itself runs on every reply: a
# component that flips the flag later must not ship PII to Teams unnoticed.
#
# Cookie and Bearer come first: their values can contain an @ or a dotted
# quad, and a narrower rule firing first would leave the rest of the header.
REDACTIONS = (
    (re.compile(r"(?i)\bcookie\b\s*[:=]\s*\S+"), "Cookie: [redacted: cookie]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [redacted: token]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[redacted: email]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[redacted: ip]"),
)


def redact(text: str) -> tuple[str, int]:
    """Replace known PII shapes with typed markers. Returns the text and count.

    Markers rather than deletion: a reader needs to know a value was removed,
    and a reviewer needs to know which rule fired.
    """
    count = 0
    for pattern, replacement in REDACTIONS:
        text, hits = pattern.subn(replacement, text)
        count += hits
    return text, count

# v1 is the shipped investigation-only shape; v2 adds per-result fixability.
# Both stay valid: a v1 document delivers findings and simply never passes
# the autofix gate (reason schema_v1).
SCHEMA_VERSIONS = (1, 2)
FIXABILITIES = ("high", "medium", "low")
STATUSES = ("investigated", "partial", "failed")
CONFIDENCES = ("high", "medium", "low")

RESULT_FIELDS = {
    "issue_id",
    "short_id",
    "status",
    "release_investigated",
    "root_cause",
    "confidence",
    "fixability",
    "evidence",
    "assumptions",
    "next_step",
    "failure_reason",
}
EVIDENCE_FIELDS = {"file", "symbol", "line", "note"}


class InvalidFindings(ValueError):
    """The findings document is unusable. Answered with a 400, never a crash."""


@dataclass(frozen=True)
class Evidence:
    file: str
    symbol: str
    line: int
    note: str


@dataclass(frozen=True)
class Result:
    issue_id: str
    short_id: str
    status: str
    release_investigated: str
    root_cause: str
    confidence: str
    next_step: str
    failure_reason: str
    fixability: str = ""
    evidence: tuple[Evidence, ...] = ()
    assumptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Findings:
    batch_id: str
    results: tuple[Result, ...] = field(default_factory=tuple)
    schema_version: int = 1


# Teams renders a thread reply's text through its markdown subset, so a bare
# `__init__` arrives bold: the same bug d2fa75a fixed for Adaptive Cards, in a
# code path that had never carried real content. Backtick is escaped in prose
# too, so prose can never open a code span of its own.
PROSE_DELIMITERS = ("\\", "*", "_", "`", "[", "]")

# Bounded so one enormous finding cannot exceed a Teams activity.
REPLY_LIMIT = 12_000


def escape_prose(text: str) -> str:
    """Neutralise markdown in free text before it reaches Teams."""
    for delimiter in PROSE_DELIMITERS:
        text = text.replace(delimiter, f"\\{delimiter}")
    return text


def code(text: str) -> str:
    """Wrap an identifier in a code span, where markdown does not apply."""
    return f"`{text.replace('`', '')}`"


def render_reply(result: "Result") -> tuple[str, int]:
    """Compose one thread reply. Returns the text and the redaction count.

    Identifiers go inside code spans and prose is escaped, which is why the
    schema splits `file`, `symbol`, and `line` into their own fields: a free
    text blob could not be treated differently from its own contents.
    """
    lines = [f"**Automated investigation: {escape_prose(result.short_id)}**"]

    if result.status == "investigated":
        lines.append(f"Confidence: {result.confidence}")
        if result.fixability:
            lines.append(f"Fixability: {result.fixability}")
    else:
        lines.append(f"Status: {result.status} (confidence: {result.confidence})")

    if result.release_investigated:
        lines.append(f"Release investigated: {code(result.release_investigated[:12])}")

    if result.root_cause:
        lines += ["", escape_prose(result.root_cause)]

    if result.failure_reason:
        lines += ["", f"**What failed:** {escape_prose(result.failure_reason)}"]

    if result.evidence:
        lines += ["", "**Evidence**"]
        for item in result.evidence:
            where = code(item.file) + (f":{item.line}" if item.line else "")
            symbol = f" in {code(item.symbol)}" if item.symbol else ""
            note = f" {escape_prose(item.note)}" if item.note else ""
            lines.append(f"- {where}{symbol}{note}")

    if result.assumptions:
        lines += ["", "**Assumptions**"]
        lines += [f"- {escape_prose(a)}" for a in result.assumptions]

    if result.next_step:
        lines += ["", f"**Next step:** {escape_prose(result.next_step)}"]

    # Redact after composing, so nothing routes around it by arriving in a
    # field the renderer treated specially.
    text, redactions = redact("\n".join(lines))
    return text[:REPLY_LIMIT], redactions


# The Teams reply is an Adaptive Card: the TL;DR (root cause, next step) stays
# visible and the supporting detail collapses behind a Details toggle, which
# plain-text replies cannot do (Teams strips <details> from message HTML).
# Bounds are per-block and per-list rather than one REPLY_LIMIT slice: a card
# is a tree, and truncating its serialized JSON would cut mid-structure.
CARD_TEXT_LIMIT = 1_000
CARD_ITEM_LIMIT = 8
DETAILS_ID = "details"


def reply_summary(short_id: str) -> str:
    """Plain-text toast line for a findings reply card."""
    return f"Automated investigation: {short_id}"


def clip(text: str) -> str:
    """Bound one card text block, marking the cut."""
    if len(text) <= CARD_TEXT_LIMIT:
        return text
    return text[: CARD_TEXT_LIMIT - 1] + "…"


def _evidence_line(item: Evidence) -> str:
    where = escape_markdown(item.file) + (f":{item.line}" if item.line else "")
    symbol = f" in {escape_markdown(item.symbol)}" if item.symbol else ""
    note = f" {escape_markdown(item.note)}" if item.note else ""
    return f"{where}{symbol}{note}"


def _bulleted(title: str, lines: tuple[str, ...] | list[str]) -> list[dict]:
    """A bold heading and one bounded TextBlock per (pre-escaped) bullet."""
    items: list[dict] = [
        {"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True}
    ]
    for line in lines[:CARD_ITEM_LIMIT]:
        items.append(
            {"type": "TextBlock", "text": clip(f"- {line}"), "wrap": True, "spacing": "Small"}
        )
    dropped = len(lines) - CARD_ITEM_LIMIT
    if dropped > 0:
        items.append(
            {
                "type": "TextBlock",
                "text": f"…and {dropped} more",
                "isSubtle": True,
                "spacing": "Small",
            }
        )
    return items


def _redact_card(card: dict) -> tuple[dict, int]:
    """Redact every text field in a composed card. Returns (card, count).

    Walks the finished tree, mirroring render_reply's compose-then-redact
    order: nothing routes around the pass by arriving in a block the builder
    treated specially.
    """
    total = 0

    def walk(node) -> None:
        nonlocal total
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "text" and isinstance(value, str):
                    node[key], hits = redact(value)
                    total += hits
                else:
                    walk(value)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(card)
    return card, total


def render_reply_card(result: "Result", disposition: str = "") -> tuple[dict, int]:
    """Compose one thread reply card. Returns the card and the redaction count.

    The visible portion is the TL;DR: what broke, how sure we are, and what to
    do next, plus the autofix disposition. Evidence and assumptions start
    hidden behind the Details toggle, so a thread scan reads in two paragraphs
    instead of eight. Cards render no code spans, so identifiers get the same
    markdown escaping as prose instead of `code()`.
    """
    status_bits = []
    if result.status == "investigated":
        status_bits.append(f"Confidence: {result.confidence}")
        if result.fixability:
            status_bits.append(f"Fixability: {result.fixability}")
    else:
        status_bits.append(f"Status: {result.status} (confidence: {result.confidence})")
    if result.release_investigated:
        status_bits.append(f"Release: {escape_markdown(result.release_investigated[:12])}")

    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": f"Automated investigation: {escape_markdown(result.short_id)}",
            "weight": "Bolder",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": " · ".join(status_bits),
            "isSubtle": True,
            "wrap": True,
            "spacing": "Small",
        },
    ]

    if result.root_cause:
        body.append(
            {
                "type": "TextBlock",
                "text": clip(escape_markdown(result.root_cause)),
                "wrap": True,
                "spacing": "Medium",
            }
        )

    if result.failure_reason:
        body.append(
            {
                "type": "TextBlock",
                "text": clip(f"**What failed:** {escape_markdown(result.failure_reason)}"),
                "wrap": True,
                "spacing": "Medium",
            }
        )

    if result.next_step:
        body.append(
            {
                "type": "TextBlock",
                "text": clip(f"**Next step:** {escape_markdown(result.next_step)}"),
                "wrap": True,
            }
        )

    if disposition:
        body.append(
            {
                "type": "TextBlock",
                "text": clip(escape_markdown(disposition)),
                "isSubtle": True,
                "wrap": True,
            }
        )

    details: list[dict] = []
    if result.evidence:
        details += _bulleted("Evidence", [_evidence_line(e) for e in result.evidence])
    if result.assumptions:
        details += _bulleted("Assumptions", [escape_markdown(a) for a in result.assumptions])

    card: dict = {"$schema": SCHEMA, "type": "AdaptiveCard", "version": VERSION, "body": body}
    if details:
        body.append(
            {
                "type": "Container",
                "id": DETAILS_ID,
                "isVisible": False,
                "spacing": "Medium",
                "items": details,
            }
        )
        card["actions"] = [
            {
                "type": "Action.ToggleVisibility",
                "title": "Details",
                "targetElements": [DETAILS_ID],
            }
        ]

    return _redact_card(card)


def _text(value, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InvalidFindings(f"{name} is not a string: {type(value).__name__}")
    return value


def _evidence(raw, index: int) -> Evidence:
    if not isinstance(raw, dict):
        raise InvalidFindings(f"results[{index}].evidence entry is not an object")

    unknown = set(raw) - EVIDENCE_FIELDS
    if unknown:
        raise InvalidFindings(f"unknown evidence field(s): {', '.join(sorted(unknown))}")

    try:
        line = int(raw.get("line") or 0)
    except (TypeError, ValueError):
        raise InvalidFindings(f"results[{index}].evidence line is not an integer") from None

    return Evidence(
        file=_text(raw.get("file"), "evidence.file"),
        symbol=_text(raw.get("symbol"), "evidence.symbol"),
        line=line,
        note=_text(raw.get("note"), "evidence.note"),
    )


def parse_findings(body, *, batch_id: str, known_issue_ids: set[str]) -> Findings:
    """Build a Findings from a session's POST, or raise InvalidFindings.

    Strict on purpose. A rejected document leaves its rows `awaiting`, so the
    deadline fallback still answers the thread; a leniently-accepted one would
    post whatever shape the session happened to invent.
    """
    if not isinstance(body, dict):
        raise InvalidFindings(f"body is not a JSON object: {type(body).__name__}")

    version = body.get("schema_version")
    if version not in SCHEMA_VERSIONS:
        raise InvalidFindings(f"unsupported schema_version: {version!r}")

    if body.get("batch_id") != batch_id:
        raise InvalidFindings("batch_id does not match the token's batch")

    raw_results = body.get("results")
    if not isinstance(raw_results, list) or not raw_results:
        raise InvalidFindings("results is missing or empty")

    results = []
    for index, raw in enumerate(raw_results):
        if not isinstance(raw, dict):
            raise InvalidFindings(f"results[{index}] is not an object")

        unknown = set(raw) - RESULT_FIELDS
        if unknown:
            raise InvalidFindings(
                f"unknown field(s) in results[{index}]: {', '.join(sorted(unknown))}"
            )

        issue_id = str(raw.get("issue_id") or "")
        if issue_id not in known_issue_ids:
            raise InvalidFindings(f"issue_id {issue_id!r} is not in batch {batch_id}")

        status = raw.get("status")
        if status not in STATUSES:
            raise InvalidFindings(
                f"results[{index}] status {status!r} is not one of {STATUSES}"
            )

        confidence = raw.get("confidence")
        if confidence not in CONFIDENCES:
            raise InvalidFindings(
                f"results[{index}] confidence {confidence!r} is not one of {CONFIDENCES}"
            )

        fixability = raw.get("fixability")
        if version >= 2:
            if fixability not in FIXABILITIES:
                raise InvalidFindings(
                    f"results[{index}] fixability {fixability!r} is not one of {FIXABILITIES}"
                )
        elif fixability is not None:
            raise InvalidFindings(
                f"results[{index}] carries fixability but schema_version is {version}"
            )

        assumptions = raw.get("assumptions") or []
        if not isinstance(assumptions, list):
            raise InvalidFindings(f"results[{index}] assumptions is not a list")

        results.append(
            Result(
                issue_id=issue_id,
                short_id=_text(raw.get("short_id"), "short_id"),
                status=status,
                release_investigated=_text(
                    raw.get("release_investigated"), "release_investigated"
                ),
                root_cause=_text(raw.get("root_cause"), "root_cause"),
                confidence=confidence,
                next_step=_text(raw.get("next_step"), "next_step"),
                failure_reason=_text(raw.get("failure_reason"), "failure_reason"),
                fixability=fixability or "",
                evidence=tuple(_evidence(e, index) for e in (raw.get("evidence") or [])),
                assumptions=tuple(_text(a, "assumption") for a in assumptions),
            )
        )

    return Findings(batch_id=batch_id, results=tuple(results), schema_version=version)
