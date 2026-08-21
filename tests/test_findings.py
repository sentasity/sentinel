"""Findings validation, rendering, redaction, and delivery."""

import copy

import pytest

from receiver.findings import (
    CARD_ITEM_LIMIT,
    CARD_TEXT_LIMIT,
    REPLY_LIMIT,
    InvalidFindings,
    code,
    escape_prose,
    parse_findings,
    redact,
    render_reply,
    render_reply_card,
    reply_summary,
)
from tests.conftest import load_fixture

BATCH = "6f1d2c88-0a2b-4f77-9d31-8f0d6a7c1e42"
KNOWN = {"1000000007"}


def payload(**changes):
    body = copy.deepcopy(load_fixture("findings-payload.json"))
    body.update(changes)
    return body


def test_a_well_formed_document_parses():
    doc = parse_findings(payload(), batch_id=BATCH, known_issue_ids=KNOWN)

    assert doc.batch_id == BATCH
    assert doc.results[0].short_id == "SCANNERS-7X"
    assert doc.results[0].evidence[0].line == 214


def test_a_body_that_is_not_an_object_is_rejected():
    with pytest.raises(InvalidFindings, match="not a JSON object"):
        parse_findings([], batch_id=BATCH, known_issue_ids=KNOWN)


def test_an_unsupported_schema_version_is_rejected():
    with pytest.raises(InvalidFindings, match="schema_version"):
        parse_findings(payload(schema_version=3), batch_id=BATCH, known_issue_ids=KNOWN)


def test_a_mismatched_batch_id_is_rejected():
    with pytest.raises(InvalidFindings, match="batch_id"):
        parse_findings(payload(), batch_id="other", known_issue_ids=KNOWN)


def test_an_issue_id_outside_the_batch_is_rejected():
    with pytest.raises(InvalidFindings, match="not in batch"):
        parse_findings(payload(), batch_id=BATCH, known_issue_ids={"999"})


def test_an_unknown_field_is_rejected_rather_than_ignored():
    body = load_fixture("findings-payload-invalid.json")

    with pytest.raises(InvalidFindings):
        parse_findings(body, batch_id=BATCH, known_issue_ids=KNOWN)


def test_a_bad_confidence_value_is_rejected():
    body = payload()
    body["results"][0]["confidence"] = "very high"

    with pytest.raises(InvalidFindings, match="confidence"):
        parse_findings(body, batch_id=BATCH, known_issue_ids=KNOWN)


def test_a_bad_status_value_is_rejected():
    body = payload()
    body["results"][0]["status"] = "probably-fine"

    with pytest.raises(InvalidFindings, match="status"):
        parse_findings(body, batch_id=BATCH, known_issue_ids=KNOWN)


def test_an_empty_results_list_is_rejected():
    with pytest.raises(InvalidFindings, match="results"):
        parse_findings(payload(results=[]), batch_id=BATCH, known_issue_ids=KNOWN)


def test_an_unknown_evidence_field_is_rejected():
    body = payload()
    body["results"][0]["evidence"][0]["severity"] = "high"

    with pytest.raises(InvalidFindings, match="evidence field"):
        parse_findings(body, batch_id=BATCH, known_issue_ids=KNOWN)


def test_a_failed_result_still_parses_because_it_still_gets_posted():
    body = payload()
    body["results"][0].update(
        status="failed",
        root_cause="",
        confidence="low",
        failure_reason="git checkout failed: unknown revision",
    )

    doc = parse_findings(body, batch_id=BATCH, known_issue_ids=KNOWN)

    assert doc.results[0].status == "failed"
    assert doc.results[0].failure_reason.startswith("git checkout failed")


def test_an_email_address_is_replaced_with_a_typed_marker():
    text, count = redact("user was tim@example.com when it failed")

    assert text == "user was [redacted: email] when it failed"
    assert count == 1


def test_an_ipv4_address_is_redacted():
    assert redact("from 203.0.113.44")[0] == "from [redacted: ip]"


def test_a_bearer_token_is_redacted():
    assert "[redacted: token]" in redact("Authorization: Bearer abc.def.ghi")[0]


def test_a_cookie_header_value_is_redacted():
    assert "[redacted: cookie]" in redact("Cookie: session=abc123; other=x")[0]


def test_a_version_number_is_not_mistaken_for_an_ip():
    """Over-redaction destroys the findings it is meant to protect."""
    assert redact("upgraded to 2.14.0")[0] == "upgraded to 2.14.0"


def test_a_file_path_with_a_dotted_name_survives():
    assert redact("src/example_app/scanners/recovery.py")[1] == 0


def test_a_line_reference_is_not_redacted():
    assert redact("recovery.py:214 in restore_session")[1] == 0


def test_the_count_reflects_every_rule_that_fired():
    text, count = redact("tim@example.com hit 203.0.113.44")

    assert count == 2
    assert "[redacted: email]" in text and "[redacted: ip]" in text


def test_prose_underscores_are_escaped_so_teams_stops_eating_them():
    assert escape_prose("call __init__ then restore_session") == (
        "call \\_\\_init\\_\\_ then restore\\_session"
    )


def test_a_code_span_needs_no_escaping_and_gets_none():
    assert code("restore_session") == "`restore_session`"


def test_a_backtick_inside_an_identifier_cannot_break_out_of_its_span():
    assert code("weird`name") == "`weirdname`"


def test_a_rendered_reply_names_the_issue_confidence_and_evidence():
    doc = parse_findings(payload(), batch_id=BATCH, known_issue_ids=KNOWN)

    text, redactions = render_reply(doc.results[0])

    assert "SCANNERS-7X" in text
    assert "high" in text
    assert "`src/example_app/scanners/recovery.py`" in text
    assert "`restore_session`" in text
    assert ":214" in text
    assert redactions == 0


def test_identifiers_in_prose_are_escaped_not_left_to_teams():
    doc = parse_findings(payload(), batch_id=BATCH, known_issue_ids=KNOWN)

    text, _ = render_reply(doc.results[0])

    assert "__init__" not in text
    assert "\\_\\_init\\_\\_" in text


def test_a_failed_result_renders_its_reason_rather_than_nothing():
    body = payload()
    body["results"][0].update(
        status="failed", root_cause="", failure_reason="git checkout failed: unknown revision"
    )
    doc = parse_findings(body, batch_id=BATCH, known_issue_ids=KNOWN)

    text, _ = render_reply(doc.results[0])

    assert "git checkout failed" in text


def test_a_rendered_reply_is_capped():
    body = payload()
    body["results"][0]["root_cause"] = "x" * 40_000
    doc = parse_findings(body, batch_id=BATCH, known_issue_ids=KNOWN)

    text, _ = render_reply(doc.results[0])

    assert len(text) <= REPLY_LIMIT


def test_pii_in_a_finding_is_redacted_before_it_reaches_teams():
    body = payload()
    body["results"][0]["root_cause"] = "the request from tim@example.com failed"
    doc = parse_findings(body, batch_id=BATCH, known_issue_ids=KNOWN)

    text, redactions = render_reply(doc.results[0])

    assert "tim@example.com" not in text
    assert redactions == 1


def payload_v2(**changes):
    body = copy.deepcopy(load_fixture("findings-payload-v2.json"))
    body.update(changes)
    return body


def test_a_v2_document_parses_with_fixability():
    doc = parse_findings(payload_v2(), batch_id=BATCH, known_issue_ids=KNOWN)

    assert doc.schema_version == 2
    assert doc.results[0].fixability == "high"


def test_a_v1_document_still_parses_and_reports_its_version():
    doc = parse_findings(payload(), batch_id=BATCH, known_issue_ids=KNOWN)

    assert doc.schema_version == 1
    assert doc.results[0].fixability == ""


def test_a_v2_result_without_fixability_is_rejected():
    body = payload_v2()
    del body["results"][0]["fixability"]

    with pytest.raises(InvalidFindings, match="fixability"):
        parse_findings(body, batch_id=BATCH, known_issue_ids=KNOWN)


def test_a_v1_result_carrying_fixability_is_rejected():
    body = payload()
    body["results"][0]["fixability"] = "high"

    with pytest.raises(InvalidFindings, match="fixability"):
        parse_findings(body, batch_id=BATCH, known_issue_ids=KNOWN)


def test_an_unknown_fixability_value_is_rejected():
    with pytest.raises(InvalidFindings, match="fixability"):
        parse_findings(
            payload_v2(results=[{**payload_v2()["results"][0], "fixability": "maybe"}]),
            batch_id=BATCH,
            known_issue_ids=KNOWN,
        )


def test_render_reply_shows_fixability_when_present():
    doc = parse_findings(payload_v2(), batch_id=BATCH, known_issue_ids=KNOWN)
    text, _ = render_reply(doc.results[0])

    assert "Fixability: high" in text


def result_v2(**changes):
    body = payload_v2()
    body["results"][0].update(changes)
    doc = parse_findings(body, batch_id=BATCH, known_issue_ids=KNOWN)
    return doc.results[0]


def visible_texts(card):
    """Text of every block outside the collapsed details container."""
    return [b["text"] for b in card["body"] if b.get("type") == "TextBlock"]


def details(card):
    return next(b for b in card["body"] if b.get("id") == "details")


def detail_texts(card):
    return [i["text"] for i in details(card)["items"]]


def test_the_reply_card_leads_with_the_tldr():
    card, redactions = render_reply_card(result_v2())

    assert card["body"][0]["text"] == "Automated investigation: SCANNERS-7X"
    assert card["body"][0]["weight"] == "Bolder"
    assert card["body"][1]["text"] == (
        "Confidence: high · Fixability: high · Release: 79bad4b79fb0"
    )
    assert "The restore flow swallows ClientError and reports sent." in visible_texts(card)
    assert (
        "**Next step:** mirror resend\\_token's LimitExceededException branch"
        in visible_texts(card)
    )
    assert redactions == 0


def test_the_reply_card_collapses_evidence_behind_the_details_toggle():
    card, _ = render_reply_card(result_v2())
    container = details(card)

    assert container["isVisible"] is False
    assert card["actions"] == [
        {
            "type": "Action.ToggleVisibility",
            "title": "Details",
            "targetElements": ["details"],
        }
    ]
    texts = detail_texts(card)
    assert "Evidence" in texts
    assert (
        "- src/example\\_app/services/accounts.py:214 "
        "in restore\\_session every ClientError is swallowed"
    ) in texts
    assert "Assumptions" in texts
    assert "- retries were human-driven" in texts


def test_evidence_never_renders_outside_the_details_container():
    """The whole point of the card: a thread scan reads the TL;DR only."""
    card, _ = render_reply_card(result_v2())

    assert all("accounts.py" not in t for t in visible_texts(card))


def test_a_reply_card_with_no_detail_has_no_toggle():
    result = result_v2(
        status="failed",
        root_cause="",
        evidence=[],
        assumptions=[],
        failure_reason="git checkout failed: unknown revision",
    )

    card, _ = render_reply_card(result)

    assert "actions" not in card
    assert all(b.get("id") != "details" for b in card["body"])
    assert card["body"][1]["text"].startswith("Status: failed (confidence: high)")
    assert (
        "**What failed:** git checkout failed: unknown revision" in visible_texts(card)
    )


def test_reply_card_escapes_markdown_for_teams():
    card, _ = render_reply_card(
        result_v2(root_cause="restore_session is called before __init__ finishes.")
    )

    joined = " ".join(visible_texts(card))
    assert "__init__" not in joined
    assert "\\_\\_init\\_\\_" in joined


def test_reply_card_redacts_pii_in_every_block():
    import json as jsonlib

    result = result_v2(
        root_cause="the request from tim@example.com failed",
        evidence=[
            {
                "file": "a.py",
                "symbol": "f",
                "line": 3,
                "note": "seen from 203.0.113.44",
            }
        ],
    )

    card, redactions = render_reply_card(result)

    dumped = jsonlib.dumps(card)
    assert "tim@example.com" not in dumped
    assert "203.0.113.44" not in dumped
    assert redactions == 2


def test_reply_card_carries_the_autofix_disposition():
    card, _ = render_reply_card(result_v2(), "Autofix: dispatching.")

    assert "Autofix: dispatching." in visible_texts(card)


def test_reply_card_caps_the_evidence_list_and_says_so():
    result = result_v2(
        evidence=[
            {"file": f"f{i}.py", "symbol": "s", "line": i + 1, "note": "n"}
            for i in range(CARD_ITEM_LIMIT + 4)
        ]
    )

    card, _ = render_reply_card(result)

    texts = detail_texts(card)
    bullets = [t for t in texts if t.startswith("- f")]
    assert len(bullets) == CARD_ITEM_LIMIT
    assert "…and 4 more" in texts


def test_reply_card_clips_an_enormous_root_cause():
    card, _ = render_reply_card(result_v2(root_cause="x" * 40_000))

    root_cause = next(t for t in visible_texts(card) if t.startswith("x"))
    assert len(root_cause) <= CARD_TEXT_LIMIT


def test_reply_summary_names_the_issue_for_the_toast():
    assert reply_summary("SCANNERS-7X") == "Automated investigation: SCANNERS-7X"


def test_the_rendered_reply_is_stable_and_fully_escaped():
    """A golden, so a rendering change has to be a deliberate one."""
    doc = parse_findings(payload(), batch_id=BATCH, known_issue_ids=KNOWN)

    text, _ = render_reply(doc.results[0])

    assert text == (
        "**Automated investigation: SCANNERS-7X**\n"
        "Confidence: high\n"
        "Release investigated: `efa4bbfc4e79`\n"
        "\n"
        "restore\\_session is called before \\_\\_init\\_\\_ finishes, so self.\\_client "
        "is still None.\n"
        "\n"
        "**Evidence**\n"
        "- `src/example_app/scanners/recovery.py`:214 in `restore_session` "
        "Dereferences self.\\_client with no guard.\n"
        "\n"
        "**Assumptions**\n"
        "- The traceback's top frame is the raising frame.\n"
        "\n"
        "**Next step:** Move the \\_client assignment above the restore\\_session call."
    )
