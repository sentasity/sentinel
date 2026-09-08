"""Sentry instrumentation for the receiver itself.

Reports to the `automation` Sentry project, whose alert rules are email-only:
routing the delivery path's own errors back through the delivery path would
loop, and would go silent exactly when the receiver is broken.
"""

from __future__ import annotations

import sentry_sdk

SERVICE_TAG = "sentinel-receiver"

# A failed Teams post is caught and answered with a 500, so the Lambda
# invocation succeeds and the AWS/Lambda Errors metric stays at zero. This
# token is logged on that path and extracted by a CloudWatch metric filter, so
# it is the delivery-failure alarm's only signal. The CDK stack imports it
# rather than repeating the string, because a filter that no longer matches
# would fail exactly the way the alarm it feeds is meant to prevent.
DELIVERY_FAILURE_MARKER = "ALERT_DELIVERY_FAILED"

# The subscription's weekly window is spent. Nobody chose this, so it is loud.
# Its deliberate counterpart, a paused routine, is counted and never alarmed:
# alarming on an operator's own budget kill switch would train them to ignore
# this alarm too. Imported by the CDK stack rather than repeated, for the same
# reason as the marker above.
WINDOW_EXHAUSTED_MARKER = "SUBSCRIPTION_WINDOW_EXHAUSTED"

# The stored prompt and the receiver's schema disagreed. That is a bug in one
# of the two, not a budget event, and it leaves a thread with no findings.
FINDINGS_REJECTED_MARKER = "FINDINGS_REJECTED"


# Bounded so a slow or unreachable Sentry cannot eat the invocation. The
# handler's own timeout is 30s and delivery has already happened by the time
# this runs, but an alert that posted and then timed out flushing would look
# like a failure to Sentry and be retried.
FLUSH_TIMEOUT_SECONDS = 2.0


def flush_sentry() -> None:
    """Drain queued events before the Lambda environment freezes.

    The SDK sends on a background worker thread. Lambda freezes that thread the
    moment the handler returns, so anything still queued is discarded rather
    than delayed. The AWS Lambda integration would normally handle this, but it
    patches the handler function at init time and `init_sentry` runs at module
    import, before `lambda_handler` is defined. Draining explicitly avoids
    depending on that import order.

    A no-op when the SDK was never initialised, which is the case with no DSN.
    """
    sentry_sdk.flush(timeout=FLUSH_TIMEOUT_SECONDS)


def init_sentry(dsn: str, environment: str) -> None:
    """Initialise the SDK. A missing DSN disables reporting rather than failing."""
    if not dsn:
        return

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        traces_sample_rate=0.0,
        send_default_pii=False,
    )
    sentry_sdk.set_tag("service", SERVICE_TAG)

# A probe session's self-report. Not alarmed: the probe is a human-run gate,
# and its pass condition is agreement between this log line and the session's
# own transcript, which a person reads at rollout.
PROBE_MARKER = "PROBE_REPORT"

# Probe reports are small; anything larger is truncated so an unauthenticated
# endpoint cannot be used to flood the log group.
PROBE_LOG_LIMIT = 4000

# Autofix. Dispatched and declined are counted, never alarmed:
# declines are the design's common case. Failed IS alarmed: a failed or
# timed-out dispatch left a thread that was promised a fix attempt.
AUTOFIX_DISPATCHED_MARKER = "AUTOFIX_DISPATCHED"
AUTOFIX_DECLINED_MARKER = "AUTOFIX_DECLINED"
AUTOFIX_FAILED_MARKER = "AUTOFIX_FAILED"
