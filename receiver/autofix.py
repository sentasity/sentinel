"""The autofix gate, the callback vocabulary, and the fix payload rules."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch

from receiver.config import ReceiverConfig
from receiver.findings import Findings, Result

# Ranks for the two gate signals; shared shape with findings.CONFIDENCES.
LEVELS = {"low": 0, "medium": 1, "high": 2}

# Paths no finding may cite and no fix may touch, regardless of operator
# config. A workflow-file change pushed to a branch runs in CI with the
# repository's secrets before any human reviews the PR. The gate applies
# this to the paths a finding claimed; `parse_fix_payload` applies it to
# the paths a fix actually carries, before any GitHub call is made.
FORBIDDEN_PATHS = (".github/*",)

# How long the session's fix phase has to call back before the sweep fails
# the grant. The session holds no credential, so nothing expires under it;
# an hour is the budget a contained fix gets before the thread is told it
# never reported.
CALLBACK_DEADLINE_SECONDS = 3600

# How long the receiver has, once it claims a record as `opening`, to
# settle it. The whole GitHub sequence runs inside one invocation with a
# 60-second ceiling, so a row still `opening` after three minutes belongs
# to an invocation that died, and the sweep fails it loudly.
OPENING_DEADLINE_SECONDS = 180

# Statuses the session may send to /autofix-result. `fix_ready` carries the
# finished fix; the receiver opens the pull request and records
# `pr_opened` itself, so a session cannot claim one.
CALLBACK_STATUSES = (
    "fix_ready",
    "aborted_drift",
    "not_reproducible",
    "declined_in_session",
    "failed",
)

# Terminal states a dispatch record settles into, each with a completion
# reply. `opening` is not among them: it is the receiver's claim while it
# builds the pull request, and the sweep fails an `opening` row that
# outlives OPENING_DEADLINE_SECONDS.
RECORD_STATUSES = (
    "pr_opened",
    "aborted_drift",
    "not_reproducible",
    "declined_in_session",
    "failed",
)

COMPLETION_REPLIES = {
    "pr_opened": "Autofix PR opened: {pr_url}",
    "aborted_drift": (
        "Autofix skipped: the base branch has moved in ways that invalidate the "
        "diagnosis."
    ),
    "not_reproducible": (
        "Autofix skipped: the root cause did not reproduce on the base branch."
    ),
    "declined_in_session": "Autofix skipped: the fix turned out larger than expected.",
    # No link, deliberately. The failure may be the session's (it reported
    # `failed`), the payload's (a rule it broke), or the receiver's (a
    # GitHub call that did not succeed), and none of those has an
    # addressable URL to offer: the detail lives in the session transcript
    # and in the receiver's own failure marker. Promising details and then
    # rendering a placeholder is worse than saying only what is true.
    "failed": "Autofix failed. No pull request was opened.",
}

# Caps on a fix_ready payload. Far above any contained fix, far below what
# would stress a 60-second invocation: twenty blob calls is a few seconds,
# and 512 KB sits well inside the Function URL's request ceiling.
FIX_FILE_LIMIT = 20
FIX_CONTENT_BYTES_LIMIT = 512 * 1024
FIX_TITLE_LIMIT = 200
FIX_BODY_LIMIT = 40_000
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def completion_reply(status: str, *, pr_url: str = "", run_url: str = "") -> str:
    """The thread reply one record status earns.

    `run_url` is still accepted, and still read and logged by the callback
    route, so a future caller that does have a run URL is not a signature
    change. No reply string interpolates it today.
    """
    return COMPLETION_REPLIES[status].format(
        pr_url=pr_url or "(missing PR URL)", run_url=run_url or "(link unavailable)"
    )


class InvalidFixPayload(ValueError):
    """A fix_ready body that broke a rule; the message names the rule."""


@dataclass(frozen=True)
class FixPayload:
    base_sha: str
    files: tuple[tuple[str, str], ...]  # (path, content), in the order sent
    title: str
    body: str


def _check_path(path: str, excluded: tuple[str, ...]) -> None:
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        raise InvalidFixPayload(f"path {path!r} is not a plain repository-relative path")
    if any(segment in (".", "..") for segment in path.split("/")):
        raise InvalidFixPayload(f"path {path!r} has a dot segment")
    if any(fnmatch(path, pattern) for pattern in excluded):
        raise InvalidFixPayload(f"path {path} is excluded")


def parse_fix_payload(body: dict, *, exclude_paths: tuple[str, ...] = ()) -> FixPayload:
    """Validate a fix_ready body; the first broken rule wins, in the order
    the contract documents them.

    Runs before any GitHub call and before the record is claimed, so a
    rejected payload costs nothing but the 400 that names the rule. The
    path policy is the same fnmatch `evaluate` applies to cited files,
    where `*` matches across `/`: that check read what a finding claimed,
    this one reads what a fix touches, in the one component whose logic
    an injected instruction cannot reach.
    """
    files = body.get("files")
    if not isinstance(files, list) or not files:
        raise InvalidFixPayload("files must be a non-empty list")
    for entry in files:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or not isinstance(entry.get("content"), str)
        ):
            raise InvalidFixPayload("each file needs a string path and string content")
    base_sha, title, pr_body = body.get("base_sha"), body.get("title"), body.get("body")
    if not all(isinstance(value, str) for value in (base_sha, title, pr_body)):
        raise InvalidFixPayload("base_sha, title, and body must be strings")
    if not COMMIT_SHA_RE.match(base_sha):
        raise InvalidFixPayload("base_sha is not a full lowercase commit sha")
    if len(files) > FIX_FILE_LIMIT:
        raise InvalidFixPayload(f"more than {FIX_FILE_LIMIT} files")
    excluded = FORBIDDEN_PATHS + tuple(exclude_paths)
    for entry in files:
        _check_path(entry["path"], excluded)
    paths = [entry["path"] for entry in files]
    if len(set(paths)) != len(paths):
        raise InvalidFixPayload("a path is listed twice")
    size = sum(len(entry["content"].encode("utf-8")) for entry in files)
    if size > FIX_CONTENT_BYTES_LIMIT:
        raise InvalidFixPayload(f"file contents exceed {FIX_CONTENT_BYTES_LIMIT} bytes")
    if not title.strip() or len(title) > FIX_TITLE_LIMIT or "\n" in title:
        raise InvalidFixPayload(
            f"title must be one non-empty line of at most {FIX_TITLE_LIMIT} characters"
        )
    if len(pr_body) > FIX_BODY_LIMIT:
        raise InvalidFixPayload(f"body exceeds {FIX_BODY_LIMIT} characters")
    return FixPayload(
        base_sha=base_sha,
        files=tuple((entry["path"], entry["content"]) for entry in files),
        title=title,
        body=pr_body,
    )


def fix_branch(short_id: str, dispatch_id: str) -> str:
    """The branch a fix lands on, computed from the record rather than
    trusted from the payload. The short id names the issue for a reader;
    the dispatch prefix keeps a re-fire from colliding with an earlier
    attempt's branch."""
    return f"autofix/{short_id.lower()}-{dispatch_id[:8]}"


@dataclass(frozen=True)
class GateDecision:
    passed: bool
    reason: str = ""  # decline reason; empty on pass

    @property
    def disposition(self) -> str:
        """The one line appended to the findings reply. Empty means silent:
        with the global kill switch off, threads read exactly as today."""
        if self.passed:
            return "Autofix: attempting a fix in this session."
        if self.reason == "disabled":
            return ""
        return f"Autofix declined: {self.reason}."


def evaluate(
    result: Result, doc: Findings, row: dict, *, cfg: ReceiverConfig, store
) -> GateDecision:
    """Ordered checks, cheapest first; the first failure wins.

    The dedupe and cap checks are conditional writes, so a pass has already
    spent them: the caller must dispatch after a pass, never re-evaluate.
    The cap is checked last so a finding declined for any other reason
    never consumes the day's budget.
    """
    if not cfg.autofix_enabled:
        return GateDecision(False, "disabled")

    if cfg.autofix_projects and row.get("project", "") not in cfg.autofix_projects:
        return GateDecision(False, "project not opted in")

    if doc.schema_version < 2:
        return GateDecision(False, "schema_v1")

    if result.status != "investigated":
        return GateDecision(False, f"status {result.status}")

    if LEVELS[result.confidence] < LEVELS[cfg.autofix_min_confidence]:
        return GateDecision(False, f"confidence {result.confidence}")

    if LEVELS[result.fixability] < LEVELS[cfg.autofix_min_fixability]:
        return GateDecision(False, f"fixability {result.fixability}")

    excluded = FORBIDDEN_PATHS + tuple(cfg.autofix_exclude_paths)
    for item in result.evidence:
        if any(fnmatch(item.file, pattern) for pattern in excluded):
            return GateDecision(False, f"excluded path {item.file}")

    if not store.claim_autofix_dedupe(
        row["issue_id"], row["environment"], row["release"]
    ):
        return GateDecision(False, "already attempted for this release")

    day = datetime.now(timezone.utc).date().isoformat()
    if not store.claim_autofix_pr(day, cfg.autofix_daily_pr_cap):
        return GateDecision(False, "daily PR cap reached")

    return GateDecision(True)
