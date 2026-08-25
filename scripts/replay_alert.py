#!/usr/bin/env python3
"""Replay a signed Sentry alert at the receiver's /sentry route.

Sentry does not re-send an alert the receiver has already seen: new-issue
rules fire on first_seen only, and a repeat on an unchanged release finds the
investigation row and stops. So the only way to make the receiver process a
known issue again is to sign a webhook body ourselves and post it.

The body is the committed webhook fixture with the target issue's fields
overridden from its stored `alert:<environment>` row, so the shape stays a
real Sentry payload rather than an invented one.

    python -m scripts.replay_alert --issue 1000000007 --environment prod \
        [--reset] [--dry-run]

Requires AWS credentials for the account holding the table and the SSM
parameters. `--reset` first deletes the investigation and autofix-dedupe rows
that would otherwise make the replay a no-op; without it a repeat issue posts
a fresh card and nothing else.

A live replay posts a real card to the environment's real Teams channel.
Use --dry-run to see the exact body and target first.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests
from botocore.exceptions import ClientError

from receiver.config import assert_ready, get_secret, load_config
from receiver.handler import sign_body
from receiver.store import AlertStore, conditional_check_failed

TIMEOUT_SECONDS = 30

# The committed capture of a real Sentry `event_alert` body, used as the
# template so a replay keeps every field the receiver does not read.
TEMPLATE = Path(__file__).resolve().parent.parent / "fixtures" / "sentry-webhook-alert.json"

# Sort keys that make a repeat replay a no-op, in the order a replay hits
# them: the investigation row stops the enqueue, and the autofix row stops the
# gate one stage later. The `alert:<environment>` row is deliberately absent;
# it is this script's own input.
BLOCKING_KEYS = ("investigation:{env}#{release}", "autofix:{env}#{release}")


class ReplayError(RuntimeError):
    """The replay cannot be built or sent."""


def conditional_error() -> ClientError:
    """A ClientError shaped like DynamoDB refusing a ConditionExpression."""
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "DeleteItem"
    )


def sentry_url(findings_url: str) -> str:
    """The /sentry route beside a configured /findings route."""
    parts = urlparse(findings_url)
    path = parts.path.rsplit("/", 1)[0] + "/sentry"
    return urlunparse(parts._replace(path=path))


def load_row(store: AlertStore, issue_id: str, environment: str) -> dict:
    """The stored alert row a replay is rebuilt from."""
    item = store.table.get_item(
        Key={"pk": f"issue:{issue_id}", "sk": f"alert:{environment}"}
    ).get("Item")
    if not item:
        raise ReplayError(
            f"no alert row for issue {issue_id} in {environment}: "
            "the receiver has never posted this issue there"
        )
    return item


def build_payload(row: dict, environment: str, org: str) -> dict:
    """The webhook fixture with one issue's real fields written over it.

    `issue_url` is set rather than left as the fixture's: resolve_issue_ref
    uses it to fetch the true short id and project slug, and the project slug
    is what the autofix gate's opt-in check reads. Leaving it stale would
    point the lookup at the fixture's issue.
    """
    payload = json.loads(TEMPLATE.read_text())
    event = payload["data"]["event"]
    issue_id = str(row["pk"]).removeprefix("issue:")
    release = row.get("release") or ""

    event["issue_id"] = issue_id
    event["issue_url"] = (
        f"https://sentry.io/api/0/organizations/{org}/issues/{issue_id}/"
    )
    event["web_url"] = row.get("web_url") or ""
    event["environment"] = environment
    event["level"] = row.get("level") or "error"
    event["title"] = row.get("title") or ""
    event["culprit"] = row.get("culprit") or ""
    event["release"] = release or None

    # parse_alert falls back to the tag list when `environment` is absent, and
    # tag_value reads whichever entry comes first. A stale tag left behind by
    # the template would route a replay to the wrong channel.
    event["tags"] = [
        [k, v] for k, v in (tuple(t) for t in event.get("tags") or [])
        if k not in ("environment", "release")
    ]
    event["tags"].append(["environment", environment])
    if release:
        event["tags"].append(["release", release])

    payload["data"]["triggered_rule"] = f"[{row.get('project') or ''}] replayed by hand"
    return payload


def reset_rows(table, issue_id: str, environment: str, release: str) -> list[str]:
    """Delete the rows that block a repeat. Returns the ones that existed.

    Each delete is conditional so an absent row is distinguishable from a
    deleted one: an unconditional delete of a mistyped key succeeds silently
    and reads as "cleared it".
    """
    deleted = []
    for template in BLOCKING_KEYS:
        sk = template.format(env=environment, release=release)
        try:
            table.delete_item(
                Key={"pk": f"issue:{issue_id}", "sk": sk},
                ConditionExpression="attribute_exists(pk)",
            )
        except ClientError as exc:
            if not conditional_check_failed(exc):
                raise
            print(f"  absent: {sk}")
            continue
        print(f"  deleted: {sk}")
        deleted.append(sk)
    return deleted


def run(issue_id: str, environment: str, reset: bool, dry_run: bool) -> int:
    cfg = load_config()
    assert_ready(cfg)
    if environment not in cfg.environments:
        print(
            f"error: {environment} is not a served environment "
            f"({', '.join(cfg.environments)})",
            file=sys.stderr,
        )
        return 2

    store = AlertStore(cfg.table_name)
    try:
        row = load_row(store, issue_id, environment)
    except ReplayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    payload = build_payload(row, environment, cfg.sentry_org)
    body = json.dumps(payload)
    target = sentry_url(cfg.findings_url)
    release = row.get("release") or ""

    print(f"issue {issue_id} ({row.get('short_id') or '?'}) in {environment}")
    print(f"release {release or '(none)'}")
    print(f"target {target}")

    if dry_run:
        print("\n--- body ---")
        print(json.dumps(payload, indent=2))
        print("\ndry run: nothing sent, nothing deleted.")
        return 0

    if reset:
        print("reset:")
        reset_rows(store.table, issue_id, environment, release)

    signature = sign_body(body, get_secret(cfg.secret_name("sentry-webhook-secret")))
    response = requests.post(
        target,
        data=body.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "sentry-hook-signature": signature,
            "sentry-hook-resource": "event_alert",
        },
        timeout=TIMEOUT_SECONDS,
    )
    print(f"POST /sentry -> {response.status_code} {(response.text or '')[:200]}")

    if response.status_code != 200:
        return 1
    print("replayed. Watch the channel for a new card, then the receiver log.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", required=True, help="Sentry numeric issue id")
    parser.add_argument("--environment", required=True, help="e.g. prod")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="first delete the investigation and autofix-dedupe rows",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the body and target; send nothing, delete nothing",
    )
    args = parser.parse_args()
    raise SystemExit(run(args.issue, args.environment, args.reset, args.dry_run))


if __name__ == "__main__":
    main()
