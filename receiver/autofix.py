"""The autofix gate: which delivered findings earn a fix attempt."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch

from receiver.config import ReceiverConfig
from receiver.findings import Findings, Result

# Ranks for the two gate signals; shared shape with findings.CONFIDENCES.
LEVELS = {"low": 0, "medium": 1, "high": 2}

# Paths no finding may cite, regardless of operator config. A workflow-file
# change pushed to a branch runs in CI with the repository's secrets before
# any human reviews the PR. The minted token cannot push one anyway
# (github_app.AUTOFIX_PERMISSIONS grants no `workflows`), so this check is
# the early, legible decline rather than the enforcement.
FORBIDDEN_PATHS = (".github/*",)

# How long the session's fix phase has to call back before the sweep fails
# the grant. Matches the vended installation token's one-hour lifetime: a
# fix that outlives its credential has failed anyway.
CALLBACK_DEADLINE_SECONDS = 3600

# Statuses the workflow may report; the callback route validates against
# this exact set, and each earns the completion reply below.
CALLBACK_STATUSES = (
    "pr_opened",
    "aborted_drift",
    "not_reproducible",
    "declined_in_session",
    "failed",
)

COMPLETION_REPLIES = {
    "pr_opened": "Autofix PR opened: {pr_url}",
    "aborted_drift": (
        "Autofix skipped: develop has moved in ways that invalidate the diagnosis."
    ),
    "not_reproducible": "Autofix skipped: the root cause did not reproduce on develop.",
    "declined_in_session": "Autofix skipped: the fix turned out larger than expected.",
    "failed": "Autofix failed. Details: {run_url}",
}


def completion_reply(status: str, *, pr_url: str = "", run_url: str = "") -> str:
    """The thread reply one callback status earns."""
    return COMPLETION_REPLIES[status].format(
        pr_url=pr_url or "(missing PR URL)", run_url=run_url or "(link unavailable)"
    )


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
