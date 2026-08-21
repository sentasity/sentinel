"""DynamoDB record of every card the receiver has posted."""

from __future__ import annotations

import enum
import hashlib
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from receiver.models import SentryAlert
from receiver.sentry_api import IssueRef

CONDITIONAL_FAILED = "ConditionalCheckFailedException"

# Sparse index over rows that are waiting for something. `due_pk` is present
# only while a row is `pending` or `awaiting`; any other status removes the
# attribute and the row leaves the index. At steady state the index is empty.
DUE_INDEX = "due-index"

# Batches by reply-token hash. Written on every fired row and never removed,
# so a retried POST for an already-delivered batch stays distinguishable from
# a token that was never issued: the first is a 200 with no repost, the second
# a 401. Projects ALL, so one query returns everything the fan-out needs.
TOKEN_INDEX = "token-index"


class BatchState(enum.Enum):
    """What a reply token's batch turned out to be.

    Four states rather than two, because the caller answers each differently
    and collapsing any pair hides something worth knowing.
    """

    UNKNOWN = "unknown"      # no such token was ever issued: a forgery
    EXPIRED = "expired"      # genuine, but past its delivery deadline
    DELIVERED = "delivered"  # genuine, already answered: a retried POST
    OPEN = "open"            # genuine and live

# Statuses that keep a row in the due index, mapped to the `due_pk` they carry
# while there. Everything else is terminal for scheduling and has its index
# attributes removed.
WAITING_STATES = {"pending": "pending", "fired": "awaiting"}


def utc_now() -> str:
    """Current UTC timestamp in the project's ISO-8601 Z form."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def conditional_check_failed(exc: ClientError) -> bool:
    """Whether a ClientError is DynamoDB refusing a ConditionExpression.

    Matched on the error code rather than caught as
    `table.meta.client.exceptions.ConditionalCheckFailedException`, because
    that attribute is generated per client and cannot be referenced against a
    mocked table in tests.
    """
    return exc.response.get("Error", {}).get("Code") == CONDITIONAL_FAILED


class AlertStore:
    """One row per (issue, environment): what was posted and where it lives."""

    def __init__(self, table_name: str):
        self.table = boto3.resource("dynamodb").Table(table_name)

    @staticmethod
    def _key(issue_id: str, environment: str) -> dict[str, str]:
        return {"pk": f"issue:{issue_id}", "sk": f"alert:{environment}"}

    def put_alert(
        self,
        alert: SentryAlert,
        ref: IssueRef,
        conversation_id: str,
        message_id: str,
    ) -> dict:
        """Persist the posted card's identity so the investigation engine can reply in its thread."""
        now = utc_now()
        item = {
            **self._key(alert.issue_id, alert.environment),
            "short_id": ref.short_id,
            "project": ref.project,
            "level": alert.level,
            "title": alert.title,
            "web_url": alert.web_url,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "posted_at": now,
            "last_alert_at": now,
        }
        if alert.release:
            item["release"] = alert.release

        self.table.put_item(Item=item)
        return item

    def get_alert(self, issue_id: str, environment: str) -> dict | None:
        """Return the stored row for an issue in an environment, or None."""
        return self.table.get_item(Key=self._key(issue_id, environment)).get("Item")

    @staticmethod
    def hash_token(token: str) -> str:
        """SHA-256 of a reply token. Stored hashed, never in the clear.

        The table is readable by anything with table-level IAM, so a stored
        plaintext capability would be a durable credential rather than the
        transient, single-use, deadline-bounded one it is meant to be.
        """
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _investigation_key(issue_id: str, environment: str, release: str) -> dict[str, str]:
        return {
            "pk": f"issue:{issue_id}",
            "sk": f"investigation:{environment}#{release}",
        }

    def put_investigation(
        self,
        alert: SentryAlert,
        ref: IssueRef,
        conversation_id: str,
        message_id: str,
        due_at: str,
    ) -> bool:
        """Enqueue one investigation. Returns False when one already exists.

        The conditional write IS the skip cache. One row per issue per
        environment per release means a new issue has none, a regression after
        a deploy gets a fresh one under a new SHA, and the five-hundredth event
        on an unchanged release finds this row and stops. No Sentry call, no
        thresholds.
        """
        item = {
            **self._investigation_key(alert.issue_id, alert.environment, alert.release or ""),
            "issue_id": alert.issue_id,
            "environment": alert.environment,
            "release": alert.release or "",
            "short_id": ref.short_id,
            "project": ref.project,
            "level": alert.level,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "status": "pending",
            "attempt": 0,
            "enqueued_at": utc_now(),
            "due_pk": "pending",
            "due_at": due_at,
        }
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
        except ClientError as exc:
            if not conditional_check_failed(exc):
                raise
            return False
        return True

    def get_investigation(self, issue_id: str, environment: str, release: str) -> dict | None:
        """Return the investigation row for an issue at a release, or None."""
        key = self._investigation_key(issue_id, environment, release)
        return self.table.get_item(Key=key).get("Item")

    def advance(
        self,
        issue_id: str,
        environment: str,
        release: str,
        expected: str,
        status: str,
        *,
        due_at: str = "",
        extra: dict | None = None,
        require_absent: str = "",
    ) -> bool:
        """Move a row from `expected` to `status`. False if it had already moved.

        The condition is what makes two writers safe. The deadline sweep and
        the findings handler both target an `awaiting` row, and exactly one of
        them must win: a fallback reply followed by the findings it said were
        missing is worse than either outcome alone.

        `require_absent` narrows the claim further: the write only wins while
        the named attribute is missing from the row. The status check alone
        cannot tell a plain awaiting row from one the delivery-retry path has
        re-armed, because both are `fired`.
        """
        names = {"#s": "status"}
        values = {":expected": expected, ":status": status, ":now": utc_now()}
        sets = ["#s = :status", "updated_at = :now"]

        due_pk = WAITING_STATES.get(status)
        stays_in_index = bool(due_pk and due_at)
        if stays_in_index:
            sets += ["due_pk = :due_pk", "due_at = :due_at"]
            values[":due_pk"] = due_pk
            values[":due_at"] = due_at

        # Every extra attribute goes through a name placeholder: callers pass
        # arbitrary keys, and a bare one that happens to be a DynamoDB reserved
        # word would fail the whole update.
        for index, (key, value) in enumerate((extra or {}).items()):
            names[f"#e{index}"] = key
            sets.append(f"#e{index} = :e{index}")
            values[f":e{index}"] = value

        expression = "SET " + ", ".join(sets)
        if not stays_in_index:
            expression += " REMOVE due_pk, due_at"

        condition = "#s = :expected"
        if require_absent:
            names["#absent"] = require_absent
            condition += " AND attribute_not_exists(#absent)"

        try:
            self.table.update_item(
                Key=self._investigation_key(issue_id, environment, release),
                UpdateExpression=expression,
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if not conditional_check_failed(exc):
                raise
            return False
        return True

    def claim_notice(
        self, issue_id: str, environment: str, release: str, kind: str
    ) -> bool:
        """Reserve the one notice a row may post about `kind`. False if spent.

        Every outcome other than a fire leaves the row `pending`, so the sweep
        picks the same group up a minute later and would say the same thing
        again. The claim is per reason rather than per row: per row bounds the
        noise just as well, but a row throttled this morning and rejected this
        afternoon would leave its thread holding the reason that stopped being
        true. A conditional write rather than a read-then-write, because two
        overlapping sweeps reading first would both find no notice and both
        post one.
        """
        try:
            self.table.update_item(
                Key=self._investigation_key(issue_id, environment, release),
                UpdateExpression="SET #n = :now",
                ConditionExpression="attribute_not_exists(#n)",
                ExpressionAttributeNames={"#n": f"notice_{kind}_at"},
                ExpressionAttributeValues={":now": utc_now()},
            )
        except ClientError as exc:
            if not conditional_check_failed(exc):
                raise
            return False
        return True

    def claim_batch(self, token: str) -> tuple[BatchState, list[dict]]:
        """Look a reply token up by hash and say what state its batch is in.

        The four states get different answers from the caller, and collapsing
        any two of them loses something: an unknown token is a forgery, an
        expired one is a late-but-genuine session, and a delivered one is a
        session retrying a POST that already worked.

        Expiry is checked here against each row's own deadline rather than
        left to the sweep. The sweep only runs once a minute, so relying on it
        would leave a token usable for up to that long past the deadline it is
        documented to die at.

        The lookup is by hash, so the plaintext token never reaches DynamoDB
        and a stored row never carries a usable credential.
        """
        response = self.table.query(
            IndexName=TOKEN_INDEX,
            KeyConditionExpression="reply_token_hash = :h",
            ExpressionAttributeValues={":h": self.hash_token(token)},
        )
        rows = response.get("Items") or []
        if not rows:
            return BatchState.UNKNOWN, []

        # Liveness first. ISO-8601 Z timestamps compare correctly as strings,
        # which is why utc_now writes them in that form.
        now = utc_now()
        live = [
            r for r in rows
            if r.get("status") == "fired" and str(r.get("due_at") or "") > now
        ]
        if live:
            return BatchState.OPEN, live

        # Only a genuinely delivered row earns the quiet 200. "No live row" is
        # NOT the same as "delivered": `expire_overdue` moves a swept row to
        # `failed` and strips its `due_at`, so treating any non-fired row as
        # delivered would answer a late session with a misleading OK depending
        # purely on whether the sweep happened to tick first.
        if any(r.get("status") == "delivered" for r in rows):
            return BatchState.DELIVERED, []
        return BatchState.EXPIRED, []

    def claim_fire(self, day: str, cap: int) -> bool:
        """Reserve one fire against the day's allowance. False when spent.

        A stored counter rather than a derived count: the sweep reads one row
        instead of aggregating, and the conditional increment makes the claim
        atomic across overlapping sweeps.
        """
        try:
            self.table.update_item(
                Key={"pk": f"fires:{day}", "sk": "counter"},
                UpdateExpression="SET fires = if_not_exists(fires, :zero) + :one",
                ConditionExpression="attribute_not_exists(fires) OR fires < :cap",
                ExpressionAttributeValues={":zero": 0, ":one": 1, ":cap": cap},
                ReturnValues="UPDATED_NEW",
            )
        except ClientError as exc:
            if not conditional_check_failed(exc):
                raise
            return False
        return True

    def query_due(self, state: str, now: str, limit: int = 50) -> list[dict]:
        """Rows in `state` whose `due_at` has passed, oldest first."""
        response = self.table.query(
            IndexName=DUE_INDEX,
            KeyConditionExpression="due_pk = :pk AND due_at <= :now",
            ExpressionAttributeValues={":pk": state, ":now": now},
            Limit=limit,
        )
        return response.get("Items") or []

    # ---- Autofix ----

    @staticmethod
    def _autofix_dispatch_key(dispatch_id: str) -> dict[str, str]:
        return {"pk": f"autofix:{dispatch_id}", "sk": "dispatch"}

    def claim_autofix_dedupe(self, issue_id: str, environment: str, release: str) -> bool:
        """One autofix per (issue, environment, release). False when spent.

        The conditional put IS the dedupe, same shape as put_investigation:
        a closed-unmerged PR never re-fires, and a new release is a new key.
        """
        try:
            self.table.put_item(
                Item={
                    "pk": f"issue:{issue_id}",
                    "sk": f"autofix:{environment}#{release}",
                    "claimed_at": utc_now(),
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
        except ClientError as exc:
            if not conditional_check_failed(exc):
                raise
            return False
        return True

    def claim_autofix_pr(self, day: str, cap: int) -> bool:
        """Reserve one dispatch against the day's PR cap. False when spent."""
        try:
            self.table.update_item(
                Key={"pk": f"autofix-prs:{day}", "sk": "counter"},
                UpdateExpression="SET fires = if_not_exists(fires, :zero) + :one",
                ConditionExpression="attribute_not_exists(fires) OR fires < :cap",
                ExpressionAttributeValues={":zero": 0, ":one": 1, ":cap": cap},
            )
        except ClientError as exc:
            if not conditional_check_failed(exc):
                raise
            return False
        return True

    def put_autofix_dispatch(self, record: dict, *, due_at: str) -> None:
        """Persist one dispatch: thread refs, token hash, callback deadline.

        `due_pk`/`due_at` put the record in the due index so the sweep can
        fail a dispatch whose callback never arrives.
        """
        self.table.put_item(
            Item={
                **self._autofix_dispatch_key(record["dispatch_id"]),
                **record,
                "status": "dispatched",
                "due_pk": "autofix",
                "due_at": due_at,
                "created_at": utc_now(),
            }
        )

    def get_autofix_dispatch(self, dispatch_id: str) -> dict | None:
        """Return one dispatch record, or None."""
        key = self._autofix_dispatch_key(dispatch_id)
        return self.table.get_item(Key=key).get("Item")

    def advance_autofix(
        self, dispatch_id: str, expected: str, status: str, *, extra: dict | None = None
    ) -> bool:
        """Move a dispatch from `expected` to `status`. False if already moved.

        Every advance is terminal for scheduling, so the index attributes
        are always removed; the callback route and the timeout sweep target
        the same transition and exactly one may win.
        """
        names = {"#s": "status"}
        values = {":expected": expected, ":status": status, ":now": utc_now()}
        sets = ["#s = :status", "updated_at = :now"]
        for index, (key, value) in enumerate((extra or {}).items()):
            names[f"#e{index}"] = key
            sets.append(f"#e{index} = :e{index}")
            values[f":e{index}"] = value

        try:
            self.table.update_item(
                Key=self._autofix_dispatch_key(dispatch_id),
                UpdateExpression="SET " + ", ".join(sets) + " REMOVE due_pk, due_at",
                ConditionExpression="#s = :expected",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if not conditional_check_failed(exc):
                raise
            return False
        return True
