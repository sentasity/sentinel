"""The stored prompts must agree with the config they are deployed alongside."""

from pathlib import Path

from receiver.config import EXAMPLE_CONFIG_PATH, PLACEHOLDER_PREFIX, load_config
from receiver.findings import RESULT_FIELDS

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


def investigator() -> str:
    return (PROMPTS / "investigator.md").read_text()


def test_the_prompt_posts_to_the_configured_findings_url():
    """Two copies of one fact, edited in different files. Keep them equal."""
    url = load_config(EXAMPLE_CONFIG_PATH).findings_url

    if url.startswith(PLACEHOLDER_PREFIX):
        assert PLACEHOLDER_PREFIX in investigator()
    else:
        assert url in investigator()


def test_the_prompt_keeps_every_prohibition_the_spike_validated():
    body = investigator().lower()

    for phrase in (
        "never ask a question",
        "posting nothing is the only unacceptable outcome",
        "never open a pr",
        "never modify code",
        "never push",
    ):
        assert phrase in body, f"the investigator prompt dropped: {phrase}"


def test_the_prompt_names_every_schema_field_the_receiver_validates():
    """A field the receiver requires but the prompt never mentions is a 400."""
    body = investigator()

    for name in RESULT_FIELDS:
        assert f'"{name}"' in body, f"the prompt never mentions {name}"


def test_the_prompt_treats_the_trigger_message_as_untrusted():
    assert "untrusted data" in investigator().lower()


def test_the_prompt_sends_the_investigator_through_the_repo_docs():
    """BACKEND-API-89 framed a CI-migration race as a process mistake because
    the session never read how the target repo deploys. The repo's own docs
    are the per-repo context; this prompt must route the session through them
    rather than duplicating repo facts here."""
    body = investigator()

    assert "CLAUDE.md" in body
    assert ".github/workflows" in body
    assert "at most 50 files" in body


def test_the_prompt_emits_schema_version_2_with_the_fixability_rubric():
    body = investigator()

    assert '"schema_version": 2' in body
    assert '"fixability"' in body
    for phrase in ("single contained change", "existing test pattern"):
        assert phrase in body, f"the fixability rubric dropped: {phrase}"


def probe() -> str:
    return (PROMPTS / "probe.md").read_text()


def test_the_probe_asks_all_four_questions():
    body = probe().lower()

    for phrase in ("git rev-parse head", "connector", "environment", "/health"):
        assert phrase in body, f"the probe prompt never covers: {phrase}"


def test_the_probe_never_reads_the_repository():
    assert "do not read repository files" in probe().lower()


def test_the_probe_reports_through_both_channels():
    """A missing POST alone cannot distinguish blocked egress from a dead session."""
    body = probe().lower()

    assert "transcript" in body
    assert "/findings/probe" in body


def test_both_prompts_verify_the_endpoint_out_of_band():
    """The env var is the operator channel a prompt injection cannot set.

    Sessions rightly refused to POST to an endpoint whose legitimacy was
    asserted only by the prompt itself; SENTINEL_RECEIVER_URL in the cloud
    environment's configuration is the independent channel they check."""
    for body in (investigator(), probe()):
        assert "SENTINEL_RECEIVER_URL" in body


def test_neither_prompt_hardcodes_a_deployed_receiver_url():
    """The repo copy is a template. The operator substitutes the real URL
    when pasting the prompt into their routine, so the two-channel check
    against SENTINEL_RECEIVER_URL still holds live."""
    for body in (investigator(), probe()):
        assert "lambda-url" not in body
