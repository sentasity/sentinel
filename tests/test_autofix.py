"""The autofix gate: ordered checks, disposition lines, completion replies."""

import re
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from receiver import autofix
from receiver.autofix import (
    RECORD_STATUSES,
    GateDecision,
    completion_reply,
    evaluate,
)
from receiver.config import load_config
from receiver.findings import parse_findings
from tests.conftest import load_fixture
from tests.test_config import AUTOFIX, VALID, write

BATCH = "6f1d2c88-0a2b-4f77-9d31-8f0d6a7c1e42"
KNOWN = {"1000000007"}

ROW = {
    "issue_id": "1000000007",
    "environment": "staging",
    "release": "79bad4b79fb044dc6386fa690aae2bc3a6ebcc29",
    "project": "checkout",
    "conversation_id": "conv-1",
    "message_id": "msg-9",
}


def doc_v2():
    return parse_findings(
        load_fixture("findings-payload-v2.json"), batch_id=BATCH, known_issue_ids=KNOWN
    )


def open_store():
    store = MagicMock()
    store.claim_autofix_dedupe.return_value = True
    store.claim_autofix_pr.return_value = True
    return store


def cfg_enabled(tmp_path, body=None):
    return load_config(write(tmp_path, body or (VALID + AUTOFIX)))


def test_a_confident_contained_finding_passes(tmp_path):
    doc = doc_v2()

    decision = evaluate(doc.results[0], doc, ROW, cfg=cfg_enabled(tmp_path), store=open_store())

    assert decision.passed
    assert decision.disposition == "Autofix: attempting a fix in this session."


def test_disabled_gate_declines_with_no_disposition_line(tmp_path):
    doc = doc_v2()
    cfg = cfg_enabled(tmp_path, VALID + AUTOFIX.replace("enabled: true", "enabled: false"))

    decision = evaluate(doc.results[0], doc, ROW, cfg=cfg, store=open_store())

    assert not decision.passed
    assert decision.reason == "disabled"
    assert decision.disposition == ""


def test_an_unlisted_project_is_declined(tmp_path):
    doc = doc_v2()

    decision = evaluate(
        doc.results[0], doc, {**ROW, "project": "frontend"},
        cfg=cfg_enabled(tmp_path), store=open_store(),
    )

    assert decision.reason == "project not opted in"


def test_an_empty_allowlist_opts_in_every_project(tmp_path):
    body = (VALID + AUTOFIX).replace("projects:\n    - checkout", "projects: []")
    doc = doc_v2()

    decision = evaluate(
        doc.results[0], doc, {**ROW, "project": "frontend"},
        cfg=cfg_enabled(tmp_path, body), store=open_store(),
    )

    assert decision.passed


def test_a_v1_document_is_declined_as_schema_v1(tmp_path):
    doc = parse_findings(
        load_fixture("findings-payload.json"), batch_id=BATCH, known_issue_ids=KNOWN
    )

    decision = evaluate(doc.results[0], doc, ROW, cfg=cfg_enabled(tmp_path), store=open_store())

    assert decision.reason == "schema_v1"


def test_a_medium_confidence_finding_is_declined(tmp_path):
    doc = doc_v2()
    result = replace(doc.results[0], confidence="medium")

    decision = evaluate(result, doc, ROW, cfg=cfg_enabled(tmp_path), store=open_store())

    assert decision.reason == "confidence medium"


def test_fixability_below_the_threshold_is_declined(tmp_path):
    doc = doc_v2()
    result = replace(doc.results[0], fixability="low")

    decision = evaluate(result, doc, ROW, cfg=cfg_enabled(tmp_path), store=open_store())

    assert decision.reason == "fixability low"


def test_an_excluded_path_declines(tmp_path):
    body = (VALID + AUTOFIX).replace('- "infra/**"', '- "src/**"')
    doc = doc_v2()

    decision = evaluate(doc.results[0], doc, ROW, cfg=cfg_enabled(tmp_path, body), store=open_store())

    assert decision.reason.startswith("excluded path")


def test_a_repeat_release_is_declined_by_dedupe(tmp_path):
    doc = doc_v2()
    store = open_store()
    store.claim_autofix_dedupe.return_value = False

    decision = evaluate(doc.results[0], doc, ROW, cfg=cfg_enabled(tmp_path), store=store)

    assert decision.reason == "already attempted for this release"


def test_a_spent_daily_cap_declines_last(tmp_path):
    doc = doc_v2()
    store = open_store()
    store.claim_autofix_pr.return_value = False

    decision = evaluate(doc.results[0], doc, ROW, cfg=cfg_enabled(tmp_path), store=store)

    assert decision.reason == "daily PR cap reached"
    store.claim_autofix_dedupe.assert_called_once()


def test_completion_replies_cover_every_record_status():
    for status in RECORD_STATUSES:
        text = completion_reply(status, pr_url="https://pr", run_url="https://run")
        assert text

    assert "https://pr" in completion_reply("pr_opened", pr_url="https://pr")


def test_no_completion_reply_names_a_branch():
    """The base branch is operator config, so a reply that hardcodes one name
    tells every other deployment something false. These strings are read by a
    human in a chat thread, so they describe the branch by its role instead."""
    for status in RECORD_STATUSES:
        text = completion_reply(status, pr_url="https://pr", run_url="https://run")
        for branch in ("develop", "main", "master", "trunk"):
            assert branch not in text.lower(), f"{status} reply names {branch!r}"


def test_a_github_directory_citation_is_declined_regardless_of_config(tmp_path):
    # exclude_paths is emptied here on purpose: the decline must come from
    # the hard-coded FORBIDDEN_PATHS, not from operator config.
    payload = load_fixture("findings-payload-v2.json")
    payload["results"][0]["evidence"][0]["file"] = ".github/workflows/deploy.yml"
    doc = parse_findings(payload, batch_id=BATCH, known_issue_ids=KNOWN)
    body = (VALID + AUTOFIX).replace('- "infra/**"', "")
    assert body != VALID + AUTOFIX  # the substitution above must actually fire
    cfg = cfg_enabled(tmp_path, body)
    assert cfg.autofix_exclude_paths == ()  # so the decline below can't come from operator config

    decision = evaluate(doc.results[0], doc, ROW, cfg=cfg, store=open_store())

    assert not decision.passed
    assert ".github/workflows/deploy.yml" in decision.reason


def test_the_pass_disposition_names_the_session_not_a_dispatch():
    assert GateDecision(True).disposition == "Autofix: attempting a fix in this session."


def test_the_failed_reply_no_longer_claims_a_workflow_ran():
    text = completion_reply("failed", run_url="https://example.test/run")

    assert "Workflow" not in text
    # The reply must not offer a link at all. Nothing sends `run_url`: the
    # session's callback body carries a dispatch id, a status, and a PR URL,
    # so a reply that interpolated one would render a placeholder every time.
    # Passing a run URL here and asserting it is absent is the point: it
    # proves the copy dropped the field rather than merely happening to have
    # nothing to fill it with.
    assert "https://example.test/run" not in text
    assert "(link unavailable)" not in text
    assert "Details" not in text


def test_no_completion_reply_promises_a_link_it_cannot_supply():
    """Every {placeholder} in a reply must be one a caller actually fills.

    `pr_url` is supplied on the `pr_opened` path; `run_url` is not supplied by
    anything, because the fix runs inside the investigating session rather
    than in a separately addressable run. A reply that names a field nobody
    sends renders its fallback text forever, which reads to the person in the
    thread as a broken link rather than as an honest absence.
    """
    for status in RECORD_STATUSES:
        text = completion_reply(status)
        assert "(link unavailable)" not in text, f"{status} reply promises a run URL"


SHA = "79bad4b79fb044dc6386fa690aae2bc3a6ebcc29"


def fix_body(**overrides) -> dict:
    body = {
        "dispatch_id": "d-1",
        "status": "fix_ready",
        "base_sha": SHA,
        "files": [
            {"path": "src/cart.py", "content": "total = 0\n"},
            {"path": "tests/test_cart.py", "content": "def test_total(): ...\n"},
        ],
        "title": "Autofix CHECKOUT-4B2: guard the empty-cart total",
        "body": "Root cause.\n\nWhat changed.\n\npytest tests/test_cart.py: passed.",
    }
    body.update(overrides)
    return body


def test_a_well_formed_fix_payload_parses_in_the_order_sent():
    payload = autofix.parse_fix_payload(fix_body())

    assert payload.base_sha == SHA
    assert payload.files == (
        ("src/cart.py", "total = 0\n"),
        ("tests/test_cart.py", "def test_total(): ...\n"),
    )
    assert payload.title.startswith("Autofix CHECKOUT-4B2")
    assert payload.body.startswith("Root cause.")


@pytest.mark.parametrize(
    ("override", "rule"),
    [
        ({"files": []}, "non-empty list"),
        ({"files": "src/cart.py"}, "non-empty list"),
        ({"files": [{"path": "src/cart.py"}]}, "string path and string content"),
        ({"base_sha": 42}, "must be strings"),
        ({"base_sha": SHA.upper()}, "full lowercase commit sha"),
        ({"base_sha": SHA[:39]}, "full lowercase commit sha"),
        (
            {"files": [{"path": f"src/f{i}.py", "content": ""} for i in range(21)]},
            "more than 20 files",
        ),
        ({"files": [{"path": "", "content": ""}]}, "not a plain repository-relative path"),
        ({"files": [{"path": "/src/cart.py", "content": ""}]}, "not a plain repository-relative path"),
        ({"files": [{"path": "src\\cart.py", "content": ""}]}, "not a plain repository-relative path"),
        ({"files": [{"path": "src/cart\x00.py", "content": ""}]}, "not a plain repository-relative path"),
        ({"files": [{"path": "./src/cart.py", "content": ""}]}, "dot segment"),
        ({"files": [{"path": "src/../.env", "content": ""}]}, "dot segment"),
        ({"files": [{"path": "src//cart.py", "content": ""}]}, "empty or dot segment"),
        ({"files": [{"path": "src/cart.py/", "content": ""}]}, "empty or dot segment"),
        ({"files": [{"path": ".github/workflows/ci.yml", "content": ""}]}, "is excluded"),
        (
            {"files": [{"path": "src/cart.py", "content": ""}, {"path": "src/cart.py", "content": "x"}]},
            "listed twice",
        ),
        ({"files": [{"path": "src/cart.py", "content": "x" * (512 * 1024 + 1)}]}, "exceed 524288 bytes"),
        ({"title": ""}, "title must be one non-empty line"),
        ({"title": "a\nb"}, "title must be one non-empty line"),
        ({"title": "t" * 201}, "title must be one non-empty line"),
        ({"body": "b" * 40_001}, "body exceeds 40000"),
    ],
)
def test_each_payload_rule_rejects_with_its_own_reason(override, rule):
    with pytest.raises(autofix.InvalidFixPayload, match=rule):
        autofix.parse_fix_payload(fix_body(**override))


def test_an_operator_excluded_path_is_rejected_like_a_forbidden_one():
    body = fix_body(files=[{"path": "infra/stack.py", "content": ""}])

    with pytest.raises(
        autofix.InvalidFixPayload, match=re.escape("path infra/stack.py is excluded")
    ):
        autofix.parse_fix_payload(body, exclude_paths=("infra/**",))


def test_the_session_cannot_claim_a_pull_request_itself():
    assert "pr_opened" not in autofix.CALLBACK_STATUSES
    assert "fix_ready" in autofix.CALLBACK_STATUSES
    assert "fix_ready" not in RECORD_STATUSES
    assert "opening" not in RECORD_STATUSES
    for status in RECORD_STATUSES:
        assert status in autofix.COMPLETION_REPLIES


def test_the_fix_branch_comes_from_the_record_not_the_payload():
    assert autofix.fix_branch("CHECKOUT-4B2", "0f3c9a1e-7b52-4d0e-a1b2") == (
        "autofix/checkout-4b2-0f3c9a1e"
    )
