"""Resolution of a Sentry issue's short id and project slug."""

from unittest.mock import patch

from receiver import sentry_api
from receiver.models import parse_alert
from tests.conftest import load_fixture


def make_alert():
    return parse_alert(load_fixture("sentry-webhook-alert.json"))


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    @property
    def ok(self):
        return 200 <= self.status_code < 300


def test_resolve_issue_ref_uses_api_values():
    sentry_api.clear_cache()
    response = FakeResponse(200, {"shortId": "SCANNERS-7X", "project": {"slug": "scanners"}})

    with patch.object(sentry_api.requests, "get", return_value=response) as get:
        ref = sentry_api.resolve_issue_ref(make_alert(), "tok")

    assert ref.short_id == "SCANNERS-7X"
    assert ref.project == "scanners"
    assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer tok"


def test_resolve_issue_ref_caches_by_issue_url():
    sentry_api.clear_cache()
    response = FakeResponse(200, {"shortId": "SCANNERS-7X", "project": {"slug": "scanners"}})

    with patch.object(sentry_api.requests, "get", return_value=response) as get:
        sentry_api.resolve_issue_ref(make_alert(), "tok")
        sentry_api.resolve_issue_ref(make_alert(), "tok")

    assert get.call_count == 1


def test_resolve_issue_ref_falls_back_on_api_failure():
    sentry_api.clear_cache()

    with patch.object(sentry_api.requests, "get", return_value=FakeResponse(403)):
        ref = sentry_api.resolve_issue_ref(make_alert(), "tok")

    assert ref.short_id == "#1000000007"
    assert ref.project == "Backend API"


def test_resolve_issue_ref_falls_back_on_transport_error():
    sentry_api.clear_cache()

    with patch.object(sentry_api.requests, "get", side_effect=OSError("boom")):
        ref = sentry_api.resolve_issue_ref(make_alert(), "tok")

    assert ref.short_id == "#1000000007"
    assert ref.project == "Backend API"


def test_project_from_rule_name_handles_missing_prefix():
    assert sentry_api.project_from_rule_name("New Issue - Prod") == ""
