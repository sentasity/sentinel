"""Sentry SDK wiring for the receiver's own errors."""

from unittest.mock import patch

from receiver.observability import (
    FLUSH_TIMEOUT_SECONDS,
    SERVICE_TAG,
    flush_sentry,
    init_sentry,
)


def test_init_sentry_configures_dsn_environment_and_service_tag():
    with patch("receiver.observability.sentry_sdk") as sdk:
        init_sentry("https://key@o0.ingest.sentry.io/1", "prod")

    kwargs = sdk.init.call_args.kwargs
    assert kwargs["dsn"] == "https://key@o0.ingest.sentry.io/1"
    assert kwargs["environment"] == "prod"
    sdk.set_tag.assert_called_once_with("service", SERVICE_TAG)


def test_service_tag_matches_the_documented_value():
    assert SERVICE_TAG == "sentinel-receiver"


def test_init_sentry_is_a_no_op_without_a_dsn():
    with patch("receiver.observability.sentry_sdk") as sdk:
        init_sentry("", "prod")

    sdk.init.assert_not_called()


def test_flush_sentry_drains_the_worker_with_a_bounded_timeout():
    """The SDK sends on a background thread that Lambda freezes on return.

    Without an explicit drain, an event queued during an invocation is
    discarded, which is why the automation project had never received one. The
    timeout is bounded so a Sentry outage cannot consume the invocation.
    """
    with patch("receiver.observability.sentry_sdk") as sdk:
        flush_sentry()

    sdk.flush.assert_called_once_with(timeout=FLUSH_TIMEOUT_SECONDS)
    assert 0 < FLUSH_TIMEOUT_SECONDS <= 5


def test_every_alarming_marker_is_a_distinct_string():
    """Two markers sharing a prefix would make one filter match the other."""
    from receiver import observability as obs

    markers = [
        obs.DELIVERY_FAILURE_MARKER,
        obs.WINDOW_EXHAUSTED_MARKER,
        obs.FINDINGS_REJECTED_MARKER,
    ]

    assert len(set(markers)) == len(markers)
    for a in markers:
        for b in markers:
            if a is not b:
                assert not a.startswith(b)
