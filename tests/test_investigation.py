"""The eligibility gate."""

import copy

from receiver.investigation import SHA, eligible
from receiver.models import parse_alert
from tests.conftest import load_fixture
from tests.test_handler import CONFIG


def alert_with(**changes):
    payload = copy.deepcopy(load_fixture("sentry-webhook-alert.json"))
    payload["data"]["event"].update(changes)
    return parse_alert(payload)


def test_an_error_with_a_release_is_eligible():
    assert eligible(alert_with(), CONFIG) == (True, "")


def test_a_warning_is_skipped_because_its_card_promised_so():
    ok, reason = eligible(alert_with(level="warning"), CONFIG)

    assert ok is False
    assert reason == "level"


def test_an_unserved_environment_is_skipped():
    assert eligible(alert_with(environment="dev"), CONFIG)[1] == "environment"


def test_a_missing_release_is_skipped_rather_than_investigated_at_head():
    assert eligible(alert_with(release=None), CONFIG)[1] == "no-release"


def test_a_release_that_is_not_a_sha_is_skipped():
    """`release_to_sha: identity` means the release must BE the commit."""
    assert eligible(alert_with(release="v1.2.3"), CONFIG)[1] == "release-not-a-sha"


def test_the_sha_pattern_accepts_exactly_forty_hex_characters():
    assert SHA.fullmatch("a" * 40)
    assert not SHA.fullmatch("a" * 39)
    assert not SHA.fullmatch("g" * 40)
