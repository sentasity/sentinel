"""The autofix gate: ordered checks, disposition lines, completion replies."""

from dataclasses import replace
from unittest.mock import MagicMock

from receiver.autofix import (
    CALLBACK_STATUSES,
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


def test_completion_replies_cover_every_callback_status():
    for status in CALLBACK_STATUSES:
        text = completion_reply(status, pr_url="https://pr", run_url="https://run")
        assert text

    assert "https://pr" in completion_reply("pr_opened", pr_url="https://pr")
    assert "https://run" in completion_reply("failed", run_url="https://run")


def test_no_completion_reply_names_a_branch():
    """The base branch is operator config, so a reply that hardcodes one name
    tells every other deployment something false. These strings are read by a
    human in a chat thread, so they describe the branch by its role instead."""
    for status in CALLBACK_STATUSES:
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
    assert "https://example.test/run" in text
