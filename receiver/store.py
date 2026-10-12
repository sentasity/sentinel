"""DynamoDB record of every card the receiver has posted."""

from __future__ import annotations

import enum
import hashlib
import json
from collections.abc import Iterable
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
WAITING_STATES = {
    "pending": "pending",
    "fired": "awaiting",
    "opening": "autofix",
    "open": "tracking",
}

# The two per-day counters the issue proxy spends: one for issues created,
# one for every write of any kind.
ISSUE_DAY_COUNTERS = ("issue-creates", "issue-writes")


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
        """Return the investigation row for an issue at a release, or None.

        Strongly consistent. Every reader here is deciding what to do about a
        conditional write that just lost, and an eventually consistent read
        can lag that write: it would report no row for the very row that
        refused the put.
        """
        key = self._investigation_key(issue_id, environment, release)
        return self.table.get_item(Key=key, ConsistentRead=True).get("Item")

    def requeue_failed(
        self,
        issue_id: str,
        environment: str,
        release: str,
        conversation_id: str,
        message_id: str,
        due_at: str,
        *,
        max_requeues: int,
        notice_kinds: Iterable[str],
    ) -> bool:
        """Return a `failed` row to `pending` under a newly posted card.

        False when the row is not failed or has been requeued `max_requeues`
        times already; the condition carries both checks so two repeat alerts
        arriving together cannot each spend the same retry. The row keeps its
        key, because the key is the skip cache, and moves its thread to the new
        card so the findings land under the alert the reader is looking at.
        Everything the earlier attempt left behind is cleared: its batch and
        reply token would answer the wrong session, a stored reply would be
        retried instead of the deadline fallback, and the notice markers would
        keep the new thread silent about an outcome it never heard.
        """
        names = {
            "#s": "status",
            "#due_pk": "due_pk",
            "#due_at": "due_at",
            "#conv": "conversation_id",
            "#msg": "message_id",
            "#attempt": "attempt",
            "#requeues": "requeues",
            "#updated": "updated_at",
            "#batch": "batch_id",
            "#token": "reply_token_hash",
            "#reply": "pending_reply",
            "#delivery": "delivery_attempt",
        }
        removes = ["#batch", "#token", "#reply", "#delivery"]
        for index, kind in enumerate(notice_kinds):
            names[f"#notice{index}"] = f"notice_{kind}_at"
            removes.append(f"#notice{index}")
        try:
            self.table.update_item(
                Key=self._investigation_key(issue_id, environment, release),
                UpdateExpression=(
                    "SET #s = :status, #due_pk = :due_pk, #due_at = :due_at, "
                    "#conv = :conversation_id, #msg = :message_id, #attempt = :zero, "
                    "#requeues = if_not_exists(#requeues, :zero) + :one, #updated = :now "
                    "REMOVE " + ", ".join(removes)
                ),
                ConditionExpression=(
                    "#s = :failed AND (attribute_not_exists(#requeues) OR #requeues < :max)"
                ),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues={
                    ":status": "pending",
                    ":failed": "failed",
                    ":due_pk": WAITING_STATES["pending"],
                    ":due_at": due_at,
                    ":conversation_id": conversation_id,
                    ":message_id": message_id,
                    ":zero": 0,
                    ":one": 1,
                    ":max": max_requeues,
                    ":now": utc_now(),
                },
            )
        except ClientError as exc:
            if not conditional_check_failed(exc):
                raise
            return False
        return True

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
        self,
        dispatch_id: str,
        expected: str,
        status: str,
        *,
        extra: dict | None = None,
        due_at: str = "",
    ) -> bool:
        """Move a dispatch from `expected` to `status`. False if already moved.

        The callback route and the timeout sweep target the same transition
        and exactly one may win. A status in WAITING_STATES given a `due_at`
        keeps the row in the due index under that fresh deadline: that is
        how the `opening` claim stays visible to the sweep without being
        expirable while the receiver is mid-sequence. Every other advance
        is terminal for scheduling and removes the index attributes.
        """
        names = {"#s": "status"}
        values = {":expected": expected, ":status": status, ":now": utc_now()}
        sets = ["#s = :status", "updated_at = :now"]
        for index, (key, value) in enumerate((extra or {}).items()):
            names[f"#e{index}"] = key
            sets.append(f"#e{index} = :e{index}")
            values[f":e{index}"] = value

        due_pk = WAITING_STATES.get(status)
        stays_in_index = bool(due_pk and due_at)
        if stays_in_index:
            sets += ["due_pk = :due_pk", "due_at = :due_at"]
            values[":due_pk"] = due_pk
            values[":due_at"] = due_at
        expression = "SET " + ", ".join(sets)
        if not stays_in_index:
            expression += " REMOVE due_pk, due_at"

        try:
            self.table.update_item(
                Key=self._autofix_dispatch_key(dispatch_id),
                UpdateExpression=expression,
                ConditionExpression="#s = :expected",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if not conditional_check_failed(exc):
                raise
            return False
        return True

    # ---- Issue tracking ----
    #
    # One grant row per delivered result, `pk = tracking:<tracking_id>`,
    # `sk = grant`, and beside it one record per distinct op the session
    # sent, `sk = op:<op key>`. Every write is conditional, so the `/issues`
    # route and the expiry sweep can both target a grant and exactly one of
    # them wins.

    @staticmethod
    def _tracking_key(tracking_id: str, sk: str = "grant") -> dict[str, str]:
        return {"pk": f"tracking:{tracking_id}", "sk": sk}

    def put_tracking_grant(self, record: dict) -> None:
        """Persist one result's tracking grant, `open` and in the due index
        until its `expires_at`, so the sweep finds it if no tracking outcome
        ever lands. `expires_at` itself is never removed: it is the token's
        liveness, checked on every call, and a tracked grant still takes
        related comments, field values and a board placement until then.

        Conditional on the row being new. A tracking id is a fresh uuid4, so
        a collision is not a case to absorb quietly; it raises.
        """
        self.table.put_item(
            Item={
                **self._tracking_key(record["tracking_id"]),
                **record,
                "status": "open",
                "due_pk": WAITING_STATES["open"],
                "due_at": record["expires_at"],
                "created_at": utc_now(),
            },
            ConditionExpression="attribute_not_exists(pk)",
        )

    def get_tracking_grant(self, tracking_id: str) -> dict | None:
        """Return one grant, or None. Strongly consistent, because every
        reader is about to decide something a conditional write just
        changed."""
        key = self._tracking_key(tracking_id)
        return self.table.get_item(Key=key, ConsistentRead=True).get("Item")

    def claim_grant_cap(self, tracking_id: str, op: str, cap: int) -> bool:
        """Spend one of this grant's attempts at `op`. False when spent.

        The counter lives on the grant row as `n_<op>`. `attribute_exists`
        keeps the update from creating a stray row for a grant that was
        never written, which an unconditioned update would do.
        """
        try:
            self.table.update_item(
                Key=self._tracking_key(tracking_id),
                UpdateExpression="SET #n = if_not_exists(#n, :zero) + :one",
                ConditionExpression=(
                    "attribute_exists(pk) AND (attribute_not_exists(#n) OR #n < :cap)"
                ),
                ExpressionAttributeNames={"#n": f"n_{op}"},
                ExpressionAttributeValues={":zero": 0, ":one": 1, ":cap": cap},
            )
        except ClientError as exc:
            if not conditional_check_failed(exc):
                raise
            return False
        return True

    def claim_daily_issue_cap(self, counter: str, day: str, cap: int) -> bool:
        """Spend one of the day's issue creates or issue writes. False when
        spent. The same conditional counter as `claim_autofix_pr`."""
        if counter not in ISSUE_DAY_COUNTERS:
            raise ValueError(f"unknown issue counter {counter!r}")
        try:
            self.table.update_item(
                Key={"pk": f"{counter}:{day}", "sk": "counter"},
                UpdateExpression="SET #c = if_not_exists(#c, :zero) + :one",
                ConditionExpression="attribute_not_exists(#c) OR #c < :cap",
                ExpressionAttributeNames={"#c": "spent"},
                ExpressionAttributeValues={":zero": 0, ":one": 1, ":cap": cap},
            )
        except ClientError as exc:
            if not conditional_check_failed(exc):
                raise
            return False
        return True

    def get_issue_op(self, tracking_id: str, key: str) -> dict | None:
        """Return the record of one op under a grant, or None."""
        record_key = self._tracking_key(tracking_id, f"op:{key}")
        return self.table.get_item(Key=record_key, ConsistentRead=True).get("Item")

    def claim_issue_op(self, tracking_id: str, key: str, *, op: str, max_attempts: int) -> bool:
        """Start one attempt at an op: create its record as `sending`, or move
        a `failed` record back to `sending` while its attempts are under
        `max_attempts`. False when the record is `sending` or `done`, or out
        of attempts.

        One conditional update covers both cases, so two retries of the same
        failed op cannot both run.
        """
        try:
            self.table.update_item(
                Key=self._tracking_key(tracking_id, f"op:{key}"),
                UpdateExpression=(
                    "SET #state = :sending, #op = :op, "
                    "#attempts = if_not_exists(#attempts, :zero) + :one, "
                    "#updated = :now REMOVE #error"
                ),
                ConditionExpression=(
                    "attribute_not_exists(pk) OR (#state = :failed AND #attempts < :max)"
                ),
                ExpressionAttributeNames={
                    "#state": "state",
                    "#op": "op",
                    "#attempts": "attempts",
                    "#updated": "updated_at",
                    "#error": "error",
                },
                ExpressionAttributeValues={
                    ":sending": "sending",
                    ":failed": "failed",
                    ":op": op,
                    ":zero": 0,
                    ":one": 1,
                    ":max": max_attempts,
                    ":now": utc_now(),
                },
            )
        except ClientError as exc:
            if not conditional_check_failed(exc):
                raise
            return False
        return True

    def settle_issue_op(
        self,
        tracking_id: str,
        key: str,
        state: str,
        *,
        result: dict | None = None,
        error: str = "",
    ) -> bool:
        """Move a `sending` op record to `done`, with the result a replay
        answers, or to `failed`, with what failed. False when it was not
        `sending`.

        The result is stored as JSON text rather than a map: DynamoDB hands
        numbers back as Decimal, which the replayed response could not
        serialize.
        """
        if state not in ("done", "failed"):
            raise ValueError(f"an op settles to done or failed, not {state!r}")
        names = {"#state": "state", "#updated": "updated_at"}
        values = {":sending": "sending", ":state": state, ":now": utc_now()}
        sets = ["#state = :state", "#updated = :now"]
        if result is not None:
            names["#result"] = "result"
            values[":result"] = json.dumps(result, sort_keys=True)
            sets.append("#result = :result")
        if error:
            names["#error"] = "error"
            values[":error"] = error
            sets.append("#error = :error")
        try:
            self.table.update_item(
                Key=self._tracking_key(tracking_id, f"op:{key}"),
                UpdateExpression="SET " + ", ".join(sets),
                ConditionExpression="#state = :sending",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if not conditional_check_failed(exc):
                raise
            return False
        return True

    def claim_tracking_issue(
        self, tracking_id: str, key: str, *, search_unavailable: bool = False
    ) -> bool:
        """Hold an open grant for the one tracking op `key` names, before its
        GitHub write. False when another op holds it or the grant is no
        longer open, so two different creates cannot both reach GitHub.

        A create that carried `search_unavailable` marks the grant here, so
        the backstop still knows the search failed if the create then does.
        """
        sets = ["#op = :key"]
        values: dict = {":key": key, ":open": "open"}
        if search_unavailable:
            sets.append("search_unavailable = :true")
            values[":true"] = True
        try:
            self.table.update_item(
                Key=self._tracking_key(tracking_id),
                UpdateExpression="SET " + ", ".join(sets),
                ConditionExpression="#s = :open AND (attribute_not_exists(#op) OR #op = :key)",
                ExpressionAttributeNames={"#s": "status", "#op": "tracking_op"},
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if not conditional_check_failed(exc):
                raise
            return False
        return True

    def release_tracking_issue(self, tracking_id: str, key: str) -> bool:
        """Drop the tracking claim `key` holds, after its op failed, so a
        corrected request can claim it. False when `key` no longer holds it."""
        try:
            self.table.update_item(
                Key=self._tracking_key(tracking_id),
                UpdateExpression="REMOVE #op",
                ConditionExpression="#op = :key",
                ExpressionAttributeNames={"#op": "tracking_op"},
                ExpressionAttributeValues={":key": key},
            )
        except ClientError as exc:
            if not conditional_check_failed(exc):
                raise
            return False
        return True

    def settle_tracked(
        self,
        tracking_id: str,
        key: str,
        *,
        issue_number: int,
        url: str,
        via: str,
        node_id: str = "",
    ) -> bool:
        """Move a grant from `open` to `tracked` in one conditional write,
        recording the issue that now tracks the alert. A create (`via`
        `file_issue`) also records the issue as this grant's own, which is
        what `set_fields` and `add_to_project` are allowed to touch.

        The grant leaves the due index, since the backstop has nothing left
        to do, and drops its tracking claim. False when the grant is no
        longer open or `key` does not hold it.
        """
        sets = [
            "#s = :tracked",
            "tracked_issue = :issue",
            "tracked_url = :url",
            "tracked_via = :via",
            "updated_at = :now",
        ]
        values = {
            ":tracked": "tracked",
            ":open": "open",
            ":key": key,
            ":issue": issue_number,
            ":url": url,
            ":via": via,
            ":now": utc_now(),
        }
        if via == "file_issue":
            sets += ["created_issue = :issue", "created_node_id = :node"]
            values[":node"] = node_id
        try:
            self.table.update_item(
                Key=self._tracking_key(tracking_id),
                UpdateExpression="SET " + ", ".join(sets) + " REMOVE #op, due_pk, due_at",
                ConditionExpression="#s = :open AND #op = :key",
                ExpressionAttributeNames={"#s": "status", "#op": "tracking_op"},
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if not conditional_check_failed(exc):
                raise
            return False
        return True

    def advance_tracking(self, tracking_id: str, expected: str, status: str) -> bool:
        """Move a grant from `expected` to `status`, out of the due index.
        The sweep's move at expiry, to `untracked` or `released`; False when
        a late tracking op settled it first."""
        try:
            self.table.update_item(
                Key=self._tracking_key(tracking_id),
                UpdateExpression="SET #s = :status, updated_at = :now REMOVE due_pk, due_at",
                ConditionExpression="#s = :expected",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":expected": expected,
                    ":status": status,
                    ":now": utc_now(),
                },
            )
        except ClientError as exc:
            if not conditional_check_failed(exc):
                raise
            return False
        return True
