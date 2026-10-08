"""The autofix gate, the callback vocabulary, and the fix payload rules."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch

from receiver.config import ReceiverConfig
from receiver.findings import Findings, Result
from receiver.patch import FileChange, PatchError, parse_patch

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
# would stress a 60-second invocation: twenty files is a few seconds of
# GitHub calls. The content cap applies to `files`, whose size is the size
# of every file touched; 512 KB sits well inside the Function URL's request
# ceiling.
FIX_FILE_LIMIT = 20
FIX_CONTENT_BYTES_LIMIT = 512 * 1024
FIX_TITLE_LIMIT = 200
FIX_BODY_LIMIT = 40_000
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# A `patch` scales with the change rather than with the files it touches,
# so this cap bounds the change itself. The ceiling it answers to is the
# Function URL's synchronous invoke payload, 6 MB: a patch at this cap is
# about 5.6 MB once base64-encoded, which still leaves room for the largest
# title and body the payload allows. A fix that needs more is not contained.
FIX_PATCH_BYTES_LIMIT = 4 * 1024 * 1024

# What a patch may make the receiver rewrite. GitHub builds a blob only from
# whole content, so the receiver reads every base file a patch modifies and
# uploads the result whole: a three-line change to a large generated file
# costs that file's size twice over, inside one invocation. This bounds that
# work, and what a patch's binary hunks may inflate to between them, well
# inside the function's memory and time.
FIX_REWRITE_BYTES_LIMIT = 48 * 1024 * 1024

# The only modes a fix may give or leave a path. A symlink can point a
# reviewer's checkout anywhere, and a submodule pin swaps in another
# repository's code under a one-line diff; neither belongs in a contained
# fix, and neither reads as what it is in a pull request.
REGULAR_FILE_MODES = ("100644", "100755")
_MODE_NAMES = {"120000": "a symlink", "160000": "a submodule", "040000": "a directory"}


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
    """A validated fix. Exactly one of `files` and `changes` is non-empty,
    matching the form the session sent."""

    base_sha: str
    title: str
    body: str
    files: tuple[tuple[str, str], ...] = ()  # (path, content), in the order sent
    changes: tuple[FileChange, ...] = ()  # parsed from `patch`, in patch order


def _check_path(path: str, excluded: tuple[str, ...]) -> None:
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        raise InvalidFixPayload(f"path {path!r} is not a plain repository-relative path")
    if any(segment in ("", ".", "..") for segment in path.split("/")):
        raise InvalidFixPayload(f"path {path!r} has an empty or dot segment")
    if any(fnmatch(path, pattern) for pattern in excluded):
        raise InvalidFixPayload(f"path {path} is excluded")


def describe_mode(mode: str) -> str:
    """How a reason names a mode outside REGULAR_FILE_MODES."""
    return _MODE_NAMES.get(mode, f"mode {mode}")


def check_changes(
    changed: Sequence[tuple[str, ...]],
    modes: Iterable[tuple[str, str]] = (),
    *,
    excluded: tuple[str, ...],
) -> None:
    """The path and mode policy every fix obeys, whichever form it came in.

    `changed` holds one entry per changed file, naming every path that
    change touches: two for a rename, since moving a file out of a
    protected path changes that path as surely as writing to it. `modes`
    pairs a path with each mode the fix declares for it. Whole files
    declare none; the receiver keeps the mode a file already has.
    """
    if len(changed) > FIX_FILE_LIMIT:
        raise InvalidFixPayload(f"more than {FIX_FILE_LIMIT} files")
    paths = [path for touched in changed for path in touched]
    for path in paths:
        _check_path(path, excluded)
    if len(set(paths)) != len(paths):
        raise InvalidFixPayload("a path is listed twice")
    for path, mode in modes:
        if mode not in REGULAR_FILE_MODES:
            article = "has" if mode not in _MODE_NAMES else "is"
            raise InvalidFixPayload(f"path {path} {article} {describe_mode(mode)}")


def _decode_patch(value) -> bytes:
    if not isinstance(value, str):
        raise InvalidFixPayload("patch must be a base64 string")
    # Line-wrapped base64 is what `base64` prints by default; the breaks
    # carry nothing, so they cost the session nothing either.
    compact = "".join(value.split())
    if len(compact) > (FIX_PATCH_BYTES_LIMIT + 2) // 3 * 4:
        raise InvalidFixPayload(f"patch exceeds {FIX_PATCH_BYTES_LIMIT} bytes")
    try:
        data = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidFixPayload("patch is not base64") from exc
    if len(data) > FIX_PATCH_BYTES_LIMIT:
        raise InvalidFixPayload(f"patch exceeds {FIX_PATCH_BYTES_LIMIT} bytes")
    return data


def parse_fix_payload(body: dict, *, exclude_paths: tuple[str, ...] = ()) -> FixPayload:
    """Validate a fix_ready body; the first broken rule wins, in the order
    the contract documents them.

    The fix arrives in one of two forms. `files` carries the whole content
    of every changed file. `patch` carries base64 of `git diff --binary`
    output taken against `base_sha`, so its size follows the change rather
    than the files: a few lines changed in a large generated file is a
    small patch. Both forms pass through `check_changes`, one path and mode
    policy, so neither can carry what the other refuses.

    Runs before any GitHub call and before the record is claimed, so a
    rejected payload costs nothing but the 400 that names the rule. The
    path policy is the same fnmatch `evaluate` applies to cited files,
    where `*` matches across `/`: that check read what a finding claimed,
    this one reads what a fix touches, in the one component whose logic
    an injected instruction cannot reach. What a patch can only be checked
    against, the base tree, is checked when the receiver applies it.
    """
    files, patch = body.get("files"), body.get("patch")
    if (files is None) == (patch is None):
        raise InvalidFixPayload("send exactly one of files or patch")
    if files is not None:
        if not isinstance(files, list) or not files:
            raise InvalidFixPayload("files must be a non-empty list")
        for entry in files:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("path"), str)
                or not isinstance(entry.get("content"), str)
            ):
                raise InvalidFixPayload("each file needs a string path and string content")
    else:
        data = _decode_patch(patch)
    base_sha, title, pr_body = body.get("base_sha"), body.get("title"), body.get("body")
    if not all(isinstance(value, str) for value in (base_sha, title, pr_body)):
        raise InvalidFixPayload("base_sha, title, and body must be strings")
    if not COMMIT_SHA_RE.match(base_sha):
        raise InvalidFixPayload("base_sha is not a full lowercase commit sha")
    excluded = FORBIDDEN_PATHS + tuple(exclude_paths)
    changes: tuple[FileChange, ...] = ()
    if files is not None:
        check_changes([(entry["path"],) for entry in files], excluded=excluded)
        size = sum(len(entry["content"].encode("utf-8")) for entry in files)
        if size > FIX_CONTENT_BYTES_LIMIT:
            raise InvalidFixPayload(f"file contents exceed {FIX_CONTENT_BYTES_LIMIT} bytes")
    else:
        try:
            changes = parse_patch(data, max_binary_bytes=FIX_REWRITE_BYTES_LIMIT)
        except PatchError as exc:
            raise InvalidFixPayload(str(exc)) from exc
        check_changes(
            [change.paths for change in changes],
            [mode for change in changes for mode in change.modes],
            excluded=excluded,
        )
    if not title.strip() or len(title) > FIX_TITLE_LIMIT or "\n" in title:
        raise InvalidFixPayload(
            f"title must be one non-empty line of at most {FIX_TITLE_LIMIT} characters"
        )
    if len(pr_body) > FIX_BODY_LIMIT:
        raise InvalidFixPayload(f"body exceeds {FIX_BODY_LIMIT} characters")
    return FixPayload(
        base_sha=base_sha,
        title=title,
        body=pr_body,
        files=tuple((entry["path"], entry["content"]) for entry in files or ()),
        changes=changes,
    )


def fix_branch(short_id: str, dispatch_id: str) -> str:
    """The branch a fix lands on, computed from the record rather than
    trusted from the payload. The short id names the issue for a reader;
    the dispatch prefix keeps a re-fire from colliding with an earlier
    attempt's branch."""
    return f"autofix/{short_id.lower()}-{dispatch_id[:8]}"


def with_fixes_line(text: str, short_id: str) -> str:
    """`text` closed by a `Fixes <short id>` line, which Sentry reads in a
    commit message or a pull request description to resolve the issue in
    the first release that carries the fix. The short id comes from the
    record, never the payload: which issue a merge resolves is the
    receiver's to say, not the session's. Applied to both the commit and
    the pull request, so the line survives however the branch is merged.
    """
    if not short_id:
        return text
    line = f"Fixes {short_id}"
    return f"{text.rstrip()}\n\n{line}" if text.strip() else line


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
