#!/usr/bin/env python3
"""Migrate Sentry alert workflows from the stock Teams action to sentinel.

    python -m scripts.migrate_rules apply    --project checkout --environment prod
    python -m scripts.migrate_rules rollback --backup fixtures/rule-backup-2026-08-12.json

`apply` always writes a backup of every workflow it is about to touch before it
touches any of them, and refuses to run if that backup cannot be written.

Sentry's UI and docs call these Monitors and Alerts; the API calls them
detectors and workflows. This module uses the API's names. The legacy
`/projects/{org}/{project}/rules/` endpoints this script once used were retired
on 17 August 2026 and now return HTTP 410.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
API_ROOT = "https://sentry.io/api/0"
TIMEOUT_SECONDS = 20

MSTEAMS_ACTION_TYPE = "msteams"
WEBHOOK_ACTION_TYPE = "webhook"
LEVEL_CONDITION_TYPE = "level"

TARGET_ENVIRONMENTS = ("prod", "staging")
UNFILTERED_ENVIRONMENT = "prod"

# A full round-trip, which the API accepts losslessly. Omitted keys are
# otherwise preserved, with one exception: `enabled` resets to true when it is
# left out, which would switch a disabled alert back on. That is why it is
# listed here even though the migration never changes it.
PUT_KEYS = (
    "name",
    "enabled",
    "environment",
    "config",
    "detectorIds",
    "triggers",
    "actionFilters",
    "owner",
)


class MigrationError(RuntimeError):
    """A workflow is not in the shape the migration expects. Never worked around."""


def action_identity(action: dict) -> tuple:
    """What makes two actions the same destination.

    The server assigns an `id` to every action, so the stored template has
    none and whole-dict equality never matches. Comparing type plus target is
    what stops an already-present action being duplicated.
    """
    config = action.get("config") or {}
    return (action.get("type"), config.get("targetIdentifier"))


def workflow_actions(workflow: dict) -> list[dict]:
    """Every action on a workflow, flattened out of its action filters."""
    return [a for af in workflow.get("actionFilters") or [] for a in af.get("actions") or []]


def swap_workflow_action(workflow: dict, action_template: dict) -> dict:
    """Return a copy of `workflow` with its Teams action replaced by sentinel.

    Raises MigrationError when no Msteams action is present, which means the
    workflow was already migrated or is not one this tool should touch.
    Silently no-oping would make a partial migration look complete.
    """
    template_identity = action_identity(action_template)
    filters: list[dict] = []
    swapped = False

    for action_filter in workflow.get("actionFilters") or []:
        actions: list[dict] = []
        for action in action_filter.get("actions") or []:
            if action.get("type") == MSTEAMS_ACTION_TYPE:
                if not swapped:
                    actions.append(copy.deepcopy(action_template))
                    swapped = True
                continue
            if action_identity(action) == template_identity:
                continue
            actions.append(copy.deepcopy(action))
        filters.append({**copy.deepcopy(action_filter), "actions": actions})

    if not swapped:
        raise MigrationError(
            f"workflow {workflow.get('name')!r} has no Msteams action to swap"
        )

    return {**copy.deepcopy(workflow), "actionFilters": filters}


def classify(workflow: dict, action_template: dict) -> str:
    """One of `pending`, `migrated`, or `unexpected`.

    The migration is already partially applied, so a slice containing
    already-migrated workflows must proceed rather than abort. Anything
    carrying neither action is still refused: it is not a shape this tool
    understands and guessing would corrupt a live rule.
    """
    actions = workflow_actions(workflow)
    if any(a.get("type") == MSTEAMS_ACTION_TYPE for a in actions):
        return "pending"
    if any(action_identity(a) == action_identity(action_template) for a in actions):
        return "migrated"
    return "unexpected"


def remove_level_condition(workflow: dict) -> dict:
    """Return a copy of `workflow` without its `level >= error` action filter condition."""
    filters = []
    for action_filter in workflow.get("actionFilters") or []:
        conditions = [
            copy.deepcopy(c)
            for c in (action_filter.get("conditions") or [])
            if c.get("type") != LEVEL_CONDITION_TYPE
        ]
        filters.append({**copy.deepcopy(action_filter), "conditions": conditions})
    return {**copy.deepcopy(workflow), "actionFilters": filters}


def plan_workflow(workflow: dict, action_template: dict) -> dict:
    """Full transform for one workflow: swap the action, and unfilter prod.

    Triggers, cooldown frequency, environment, and owner are never modified;
    retuning alert noise is deliberately out of this migration's scope.
    """
    planned = swap_workflow_action(workflow, action_template)
    if workflow.get("environment") == UNFILTERED_ENVIRONMENT:
        planned = remove_level_condition(planned)
    return planned


def detector_projects(detectors: list[dict]) -> dict[str, str]:
    """Map detector id to the project id that owns it."""
    return {str(d["id"]): str(d["projectId"]) for d in detectors}


def workflows_for_project(
    workflows: list[dict], detectors: list[dict], project_id: str
) -> list[dict]:
    """Every workflow whose detectors belong to `project_id`.

    Workflows are org-scoped in this API, so the project binding has to come
    from the detectors they reference. A workflow referencing detectors from
    more than one project is refused rather than guessed at.
    """
    owner = detector_projects(detectors)
    found = []
    for workflow in workflows:
        projects = {owner[str(d)] for d in (workflow.get("detectorIds") or []) if str(d) in owner}
        if not projects:
            continue
        if len(projects) > 1:
            raise MigrationError(
                f"workflow {workflow['name']!r} spans projects {sorted(projects)}"
            )
        if projects.pop() == str(project_id):
            found.append(workflow)
    return found


def put_payload(workflow: dict) -> dict:
    """Reduce a fetched workflow to the keys the update endpoint accepts."""
    return {key: workflow[key] for key in PUT_KEYS if key in workflow}


def apply(
    client,
    project: str,
    action_template: dict,
    backup_path: Path,
    environments: tuple[str, ...] = TARGET_ENVIRONMENTS,
    dry_run: bool = False,
) -> dict:
    """Swap every pending workflow in `project`, backing them all up first.

    The backup is written before the first update, and a failure to write it
    aborts before anything changes: an un-backed-up migration has no rollback.

    `dry_run` exercises the read and plan path and stops there. The legacy tool
    had no such mode, which is why an endpoint retirement was first discovered
    partway through a live migration rather than before one.
    """
    workflows = workflows_for_project(
        client.list_workflows(), client.list_detectors(), client.project_id(project)
    )
    in_slice = [w for w in workflows if w.get("environment") in environments]

    buckets: dict[str, list[dict]] = {"pending": [], "migrated": [], "unexpected": []}
    for workflow in in_slice:
        buckets[classify(workflow, action_template)].append(workflow)

    if buckets["unexpected"]:
        names = ", ".join(repr(w["name"]) for w in buckets["unexpected"])
        raise MigrationError(f"refusing to touch workflows in an unknown shape: {names}")

    targets = buckets["pending"]
    # Plan everything first. A workflow that cannot be swapped must abort while
    # the previous backup is still intact and before a single live workflow has
    # been touched.
    planned = [(w["id"], put_payload(plan_workflow(w, action_template))) for w in targets]

    if dry_run:
        return {
            "updated": [],
            "skipped": [w["name"] for w in buckets["migrated"]],
            "planned": [w["name"] for w in targets],
        }

    if backup_path.exists():
        raise MigrationError(
            f"backup already exists: {backup_path}. Refusing to overwrite a "
            f"rollback artifact; pass --backup with a new path."
        )
    try:
        backup_path.write_text(json.dumps(targets, indent=2) + "\n")
    except OSError as exc:
        raise MigrationError(f"cannot write backup to {backup_path}: {exc}") from exc

    updated: list[str] = []
    for workflow_id, payload in planned:
        try:
            client.update(workflow_id, payload)
        except Exception as exc:
            # Deliberately broad: however this dies, the operator must be told
            # which live workflows already changed and where the undo file is.
            # Losing that is worse than any exception type being reclassified.
            raise MigrationError(
                f"{exc}. Already updated: {', '.join(updated) or 'none'}. "
                f"Roll back with: migrate_rules.py rollback --backup {backup_path}"
            ) from exc
        updated.append(workflow_id)

    return {
        "updated": updated,
        "skipped": [w["name"] for w in buckets["migrated"]],
    }


def rollback(client, backup_path: Path) -> list[str]:
    """Restore every workflow in a backup file to exactly its captured state."""
    workflows = json.loads(backup_path.read_text())
    if not workflows:
        raise MigrationError(f"backup is empty: {backup_path}")

    restored: list[str] = []
    for workflow in workflows:
        try:
            client.update(workflow["id"], put_payload(workflow))
        except Exception as exc:
            # Same reasoning as `apply`, and it matters more here: this is the
            # recovery path, so going blind partway through is the worst case.
            raise MigrationError(
                f"{exc}. Already restored: {', '.join(restored) or 'none'}. "
                f"Re-run the same rollback to finish; it is idempotent."
            ) from exc
        restored.append(workflow["id"])
    return restored


def default_backup_path(project: str, environments: tuple[str, ...]) -> Path:
    """A backup filename unique per project, environment slice, and second.

    The runbook applies staging and prod back to back on the same day, so a
    date-only name would have prod's backup overwrite staging's, leaving the
    staged rollout with no way back to its first slice.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    slice_name = "-".join(sorted(environments)) or "all"
    return REPO_ROOT / "fixtures" / f"rule-backup-{project}-{slice_name}-{stamp}.json"


def build_client(org: str) -> "SentryWorkflows":
    token = os.environ.get("SENTRY_ACCESS_TOKEN", "")
    if not token:
        raise MigrationError("SENTRY_ACCESS_TOKEN is unset")
    return SentryWorkflows(org, token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True, help="Sentry organization slug")
    sub = parser.add_subparsers(dest="command", required=True)

    apply_cmd = sub.add_parser("apply", help="swap workflows to sentinel")
    apply_cmd.add_argument("--project", required=True, help="Sentry project slug")
    apply_cmd.add_argument(
        "--environment", action="append", choices=TARGET_ENVIRONMENTS, default=None
    )
    apply_cmd.add_argument(
        "--action-template",
        type=Path,
        default=REPO_ROOT / "fixtures" / "sentry-workflow-action.json",
    )
    apply_cmd.add_argument("--backup", type=Path, default=None)
    apply_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="read and plan the slice, report it, and write nothing",
    )

    rollback_cmd = sub.add_parser("rollback", help="restore workflows from a backup")
    rollback_cmd.add_argument("--backup", type=Path, required=True)

    args = parser.parse_args(argv)

    try:
        client = build_client(args.org)

        if args.command == "apply":
            template = json.loads(args.action_template.read_text())
            environments = tuple(args.environment or TARGET_ENVIRONMENTS)
            backup = args.backup or default_backup_path(args.project, environments)
            if not args.dry_run:
                # Printed before anything is touched, so the undo path is on
                # screen even if the run dies partway through.
                print(f"backup: {backup}")
            result = apply(
                client, args.project, template, backup, environments, dry_run=args.dry_run
            )
            for name in result["skipped"]:
                print(f"skipped (already migrated): {name}")
            if args.dry_run:
                for name in result["planned"]:
                    print(f"would update: {name}")
            else:
                print(
                    f"updated {len(result['updated'])} workflows: "
                    f"{', '.join(result['updated'])}"
                )
        else:
            restored = rollback(client, args.backup)
            print(f"restored {len(restored)} workflows: {', '.join(restored)}")
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


class SentryWorkflows:
    """Thin wrapper over the Sentry org workflows API."""

    def __init__(self, org: str, token: str):
        self.org = org
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def _get(self, url: str):
        try:
            response = self.session.get(url, timeout=TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise MigrationError(f"GET {url} failed: {exc}") from exc
        if not response.ok:
            raise MigrationError(f"GET {url} returned HTTP {response.status_code}")
        return response.json()

    def list_workflows(self) -> list[dict]:
        return self._get(f"{API_ROOT}/organizations/{self.org}/workflows/")

    def list_detectors(self) -> list[dict]:
        return self._get(f"{API_ROOT}/organizations/{self.org}/detectors/")

    def project_id(self, slug: str) -> str:
        projects = self._get(f"{API_ROOT}/organizations/{self.org}/projects/")
        for project in projects:
            if project["slug"] == slug:
                return str(project["id"])
        raise MigrationError(f"no project {slug!r} in org {self.org!r}")

    def update(self, workflow_id: str, payload: dict) -> dict:
        url = f"{API_ROOT}/organizations/{self.org}/workflows/{workflow_id}/"
        # A dropped socket is the likeliest way a run against a remote API dies.
        # It has to reach the caller as a MigrationError, or the recovery message
        # naming the already-updated workflows never gets built.
        try:
            response = self.session.put(url, json=payload, timeout=TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise MigrationError(f"PUT {url} failed: {exc}") from exc
        if not response.ok:
            raise MigrationError(
                f"PUT {url} returned HTTP {response.status_code}: {response.text}"
            )
        return response.json()


if __name__ == "__main__":
    sys.exit(main())
