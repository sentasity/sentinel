"""Receiver configuration loading and startup assertions."""

import textwrap
from unittest.mock import MagicMock, patch

import pytest

from receiver.config import ConfigError, assert_ready, get_secret, load_config

VALID = textwrap.dedent(
    """
    environments:
      - prod
      - staging
    target_repo: acme-tools/checkout
    trigger_mode: auto
    payload_mode: issue-ids-only
    trigger:
      routine_id: trig_test
    investigation:
      debounce_seconds: 60
      max_batch_issues: 8
      daily_fire_cap: 40
      per_sweep_fire_cap: 4
      deadline_seconds: 900
      findings_url: "https://example.test/findings"
    aws:
      account: "123456789012"
      region: us-east-1
      table_name: sentinel-alerts
      alarm_email: ops@example.com
    teams:
      tenant_id: "tenant-123"
      service_url: "https://smba.trafficmanager.net/amer/"
      bot_app_id: "app-456"
      channels:
        prod: "19:prod@thread.tacv2"
        staging: "19:staging@thread.tacv2"
    webhook:
      sentry_org: acme-tools
      ssm_prefix: /sentinel
    observability:
      automation_dsn: "https://key@o0.ingest.sentry.io/1"
    """
)


BARE = textwrap.dedent(
    """
    environments:
      - prod
    aws:
      table_name: sentinel-alerts
    teams:
      tenant_id: "tenant-123"
      service_url: "https://smba.trafficmanager.net/amer/"
      bot_app_id: "app-456"
      channels:
        prod: "19:prod@thread.tacv2"
    webhook:
      ssm_prefix: /sentinel
    """
)


def write(tmp_path, body):
    path = tmp_path / "config.yaml"
    path.write_text(body)
    return path


def test_load_config_reads_the_investigation_section(tmp_path):
    cfg = load_config(write(tmp_path, VALID))

    assert cfg.target_repo == "acme-tools/checkout"
    assert cfg.trigger_mode == "auto"
    assert cfg.payload_mode == "issue-ids-only"
    assert cfg.routine_id == "trig_test"
    assert cfg.findings_url == "https://example.test/findings"
    assert cfg.debounce_seconds == 60
    assert cfg.max_batch_issues == 8
    assert cfg.daily_fire_cap == 40
    assert cfg.per_sweep_fire_cap == 4
    assert cfg.deadline_seconds == 900


def test_a_config_with_no_investigation_section_cannot_fire(tmp_path):
    """`shadow` is the default, so an incomplete config is inert, not dangerous."""
    cfg = load_config(write(tmp_path, BARE))

    assert cfg.trigger_mode == "shadow"
    assert cfg.routine_id == ""
    assert cfg.findings_url == ""
    assert cfg.deadline_seconds == 900


def test_load_config_reads_every_section(tmp_path):
    cfg = load_config(write(tmp_path, VALID))

    assert cfg.environments == ("prod", "staging")
    assert cfg.channels["prod"] == "19:prod@thread.tacv2"
    assert cfg.service_url == "https://smba.trafficmanager.net/amer/"
    assert cfg.bot_app_id == "app-456"
    assert cfg.table_name == "sentinel-alerts"
    assert cfg.ssm_prefix == "/sentinel"
    assert cfg.automation_dsn == "https://key@o0.ingest.sentry.io/1"


def test_assert_ready_tolerates_an_empty_automation_dsn(tmp_path):
    cfg = load_config(write(tmp_path, VALID.replace('"https://key@o0.ingest.sentry.io/1"', '""')))

    assert_ready(cfg)
    assert cfg.automation_dsn == ""


def test_assert_ready_accepts_a_complete_config(tmp_path):
    assert_ready(load_config(write(tmp_path, VALID)))


def test_assert_ready_rejects_environment_without_a_channel(tmp_path):
    cfg = load_config(write(tmp_path, VALID.replace('  prod: "19:prod@thread.tacv2"\n', "")))

    with pytest.raises(ConfigError, match="no Teams channel"):
        assert_ready(cfg)


def test_assert_ready_rejects_placeholder_values(tmp_path):
    cfg = load_config(write(tmp_path, VALID.replace("app-456", "REPLACE_WITH_ENTRA_APP_ID")))

    with pytest.raises(ConfigError, match="bot_app_id"):
        assert_ready(cfg)


def test_assert_ready_rejects_missing_service_url(tmp_path):
    cfg = load_config(write(tmp_path, VALID.replace('  service_url: "https://smba.trafficmanager.net/amer/"\n', "")))

    with pytest.raises(ConfigError, match="service_url"):
        assert_ready(cfg)


def test_get_secret_reads_a_decrypted_ssm_parameter():
    client = MagicMock()
    client.get_parameter.return_value = {"Parameter": {"Value": "s3cret"}}

    with patch("receiver.config.boto3.client", return_value=client):
        get_secret.cache_clear()
        assert get_secret("/sentinel/bot-client-secret") == "s3cret"

    client.get_parameter.assert_called_once_with(
        Name="/sentinel/bot-client-secret", WithDecryption=True
    )


# Dev environments are excluded because their releases are not resolvable, not
# as a matter of taste. A dev deploy path that leaves SENTRY_RELEASE unset gives
# every per-developer event `release=""`, while CI sets it for prod and staging.


def test_assert_ready_refuses_a_developer_environment(tmp_path):
    """A dev env in this list would break release-checkout on every event."""
    body = VALID.replace("  - staging", "  - staging\n  - dev").replace(
        '    staging: "19:staging@thread.tacv2"',
        '    staging: "19:staging@thread.tacv2"\n    dev: "19:dev@thread.tacv2"',
    )

    with pytest.raises(ConfigError, match="not investigable"):
        assert_ready(load_config(write(tmp_path, body)))


def test_assert_ready_names_the_offending_environment(tmp_path):
    body = VALID.replace("  - staging", "  - staging\n  - tim").replace(
        '    staging: "19:staging@thread.tacv2"',
        '    staging: "19:staging@thread.tacv2"\n    tim: "19:tim@thread.tacv2"',
    )

    with pytest.raises(ConfigError, match="tim"):
        assert_ready(load_config(write(tmp_path, body)))


def test_assert_ready_accepts_the_investigable_environments(tmp_path):
    assert_ready(load_config(write(tmp_path, VALID)))


def test_assert_ready_rejects_an_unknown_trigger_mode(tmp_path):
    cfg = load_config(write(tmp_path, VALID.replace("trigger_mode: auto", "trigger_mode: yolo")))

    with pytest.raises(ConfigError, match="trigger_mode"):
        assert_ready(cfg)


def test_assert_ready_rejects_the_stale_full_event_payload_mode(tmp_path):
    """The live config carried `full-event`, which 400s on real traffic."""
    cfg = load_config(
        write(tmp_path, VALID.replace("payload_mode: issue-ids-only", "payload_mode: full-event"))
    )

    with pytest.raises(ConfigError, match="65,536"):
        assert_ready(cfg)


def test_assert_ready_requires_a_routine_id_when_the_mode_fires(tmp_path):
    cfg = load_config(write(tmp_path, VALID.replace("routine_id: trig_test", 'routine_id: ""')))

    with pytest.raises(ConfigError, match="routine_id"):
        assert_ready(cfg)


def test_assert_ready_lets_shadow_mode_run_without_a_routine_or_url(tmp_path):
    """The Function URL is an output of the first deploy, so it cannot exist yet."""
    body = VALID.replace("trigger_mode: auto", "trigger_mode: shadow")
    body = body.replace("routine_id: trig_test", "routine_id: REPLACE_WITH_ROUTINE_ID")
    body = body.replace('findings_url: "https://example.test/findings"', 'findings_url: ""')

    assert_ready(load_config(write(tmp_path, body)))


def test_assert_ready_requires_a_target_repo(tmp_path):
    cfg = load_config(
        write(tmp_path, VALID.replace("target_repo: acme-tools/checkout", 'target_repo: ""'))
    )

    with pytest.raises(ConfigError, match="target_repo"):
        assert_ready(cfg)


AUTOFIX = textwrap.dedent(
    """
    autofix:
      enabled: true
      projects:
        - checkout
      min_confidence: high
      min_fixability: medium
      exclude_paths:
        - "infra/**"
      daily_pr_cap: 5
      app_id: "1234567"
      base_branch: main
      callback_url: "https://example.test/autofix-result"
    """
)


def test_load_config_reads_the_autofix_section(tmp_path):
    cfg = load_config(write(tmp_path, VALID + AUTOFIX))

    assert cfg.autofix_enabled is True
    assert cfg.autofix_projects == ("checkout",)
    assert cfg.autofix_min_fixability == "medium"
    assert cfg.autofix_exclude_paths == ("infra/**",)
    assert cfg.autofix_daily_pr_cap == 5
    assert cfg.autofix_app_id == "1234567"
    assert cfg.autofix_base_branch == "main"
    assert cfg.autofix_callback_url == "https://example.test/autofix-result"


def test_autofix_defaults_keep_the_gate_closed(tmp_path):
    cfg = load_config(write(tmp_path, VALID))

    assert cfg.autofix_enabled is False
    assert cfg.autofix_projects == ()
    assert cfg.autofix_min_confidence == "high"
    assert cfg.autofix_min_fixability == "high"


def test_assert_ready_rejects_a_bad_autofix_threshold(tmp_path):
    body = VALID + AUTOFIX.replace("min_fixability: medium", "min_fixability: sometimes")

    with pytest.raises(ConfigError, match="min_fixability"):
        assert_ready(load_config(write(tmp_path, body)))


def test_enabled_autofix_requires_app_id_and_callback(tmp_path):
    body = VALID + AUTOFIX.replace('app_id: "1234567"', 'app_id: ""')

    with pytest.raises(ConfigError, match="autofix.app_id"):
        assert_ready(load_config(write(tmp_path, body)))


def test_the_probe_routine_is_configured_separately_from_the_investigator(tmp_path):
    """A routine holds one prompt at a time, so the probe needs its own
    routine, and a trigger token authorises exactly one routine, so it needs
    its own token reference too."""
    # VALID is textwrap.dedent'd, so `routine_id` sits at two spaces, not the
    # six it shows at in the source literal. A search string with the wrong
    # indent is a silent no-op, which the assertion below exists to catch.
    body = VALID.replace(
        "  routine_id: trig_test",
        "  routine_id: trig_test\n  probe_routine_id: trig_probe",
    )
    assert "probe_routine_id" in body, "the probe id was never substituted in"

    cfg = load_config(write(tmp_path, body))

    assert cfg.probe_routine_id == "trig_probe"
    assert cfg.probe_routine_id != cfg.routine_id
    # Defaulted rather than required, so an existing config keeps working.
    assert cfg.probe_token_ref == "probe-trigger-token"


def test_an_absent_probe_routine_is_not_an_error(tmp_path):
    """Nothing the receiver serves reads either value, so a deployment that
    has not created a probe routine must still come up."""
    cfg = load_config(write(tmp_path, VALID))

    assert cfg.probe_routine_id == ""
    assert_ready(cfg)


def test_enabled_autofix_requires_a_base_branch(tmp_path):
    body = VALID + AUTOFIX.replace("base_branch: main", 'base_branch: ""')
    assert body != VALID + AUTOFIX  # the substitution above must actually fire

    with pytest.raises(ConfigError, match="autofix.base_branch"):
        assert_ready(load_config(write(tmp_path, body)))


def test_the_config_no_longer_has_a_dispatch_repo(tmp_path):
    cfg = load_config(write(tmp_path, VALID + AUTOFIX))

    assert not hasattr(cfg, "autofix_repo")


def test_the_new_secret_key_is_registered(tmp_path):
    from receiver.config import SECRET_KEYS

    assert "github-app-private-key" in SECRET_KEYS


def test_the_default_config_path_is_the_untracked_local_file():
    from receiver.config import DEFAULT_CONFIG_FILENAME, DEFAULT_CONFIG_PATH

    assert DEFAULT_CONFIG_FILENAME == "receiver.yaml"
    assert DEFAULT_CONFIG_PATH.name == DEFAULT_CONFIG_FILENAME
    assert DEFAULT_CONFIG_PATH.parent.name == "config"


def test_the_example_config_path_points_at_the_shipped_template():
    from receiver.config import EXAMPLE_CONFIG_PATH

    assert EXAMPLE_CONFIG_PATH.is_file()
    assert EXAMPLE_CONFIG_PATH.name == "receiver.yaml.example"


def test_the_example_config_loads_and_starts_inert():
    """A fresh clone's template must parse, and must fire nothing."""
    from receiver.config import EXAMPLE_CONFIG_PATH

    cfg = load_config(EXAMPLE_CONFIG_PATH)

    assert cfg.trigger_mode == "shadow"
    assert cfg.autofix_enabled is False


def test_the_investigator_token_ref_defaults_to_the_documented_key(tmp_path):
    cfg = load_config(write(tmp_path, VALID))

    assert cfg.token_ref == "routine-trigger-token"
    assert cfg.secret_name(cfg.token_ref) == "/sentinel/routine-trigger-token"


def test_the_investigator_token_ref_is_read_from_the_trigger_section(tmp_path):
    body = VALID.replace(
        "  routine_id: trig_test",
        "  routine_id: trig_test\n  token_ref: investigator-token",
    )
    assert "token_ref" in body, "the token ref was never substituted in"

    assert load_config(write(tmp_path, body)).token_ref == "investigator-token"


# Built at runtime so no token-shaped literal sits in the tree for the secret
# scanners to flag. Real routine trigger tokens are about this long.
TOKEN_SHAPED = "sk-" + "a" * 105


@pytest.mark.parametrize("key", ["token_ref", "probe_token_ref"])
@pytest.mark.parametrize(
    "value",
    [TOKEN_SHAPED, "sk-short", "a" * 65, "nested/key", "-leading-dash"],
    ids=["token", "sk-prefix", "too-long", "slash", "leading-dash"],
)
def test_a_token_ref_that_is_not_an_ssm_key_is_refused(tmp_path, key, value):
    """The config file ships in the Lambda bundle, so a token pasted where its
    SSM key belongs would put a credential in the deployed code."""
    body = VALID.replace(
        "  routine_id: trig_test",
        f"  routine_id: trig_test\n  {key}: {value}",
    )

    with pytest.raises(ConfigError, match=f"trigger.{key} must name an SSM key"):
        load_config(write(tmp_path, body))


def test_a_refused_token_ref_is_never_echoed(tmp_path):
    """If the value is a token, the error must not become a second copy of it."""
    body = VALID.replace(
        "  routine_id: trig_test",
        f"  routine_id: trig_test\n  token_ref: {TOKEN_SHAPED}",
    )

    with pytest.raises(ConfigError) as refused:
        load_config(write(tmp_path, body))

    assert TOKEN_SHAPED not in str(refused.value)
    assert TOKEN_SHAPED[:12] not in str(refused.value)


ISSUES = textwrap.dedent(
    """
    issues:
      enabled: true
      environments: [staging]
      proxy_url: "https://example.test/issues"
      conventions: [docs/ISSUES.md]
      issue_types: [Bug, Task]
      fields: [Priority, Effort]
      project_number: 3
      daily_create_cap: 25
      daily_write_cap: 100
    """
)

# VALID carries no autofix block, and issue filing writes as the autofix
# App, so a config that files issues needs AUTOFIX for its app_id.
FILING = VALID + AUTOFIX + ISSUES


def test_load_config_reads_the_issues_section(tmp_path):
    cfg = load_config(write(tmp_path, FILING))

    assert cfg.issues_enabled is True
    assert cfg.issues_environments == ("staging",)
    assert cfg.issues_proxy_url == "https://example.test/issues"
    assert cfg.issues_conventions == ("docs/ISSUES.md",)
    assert cfg.issues_issue_types == ("Bug", "Task")
    assert cfg.issues_fields == ("Priority", "Effort")
    assert cfg.issues_project_number == 3
    assert cfg.issues_daily_create_cap == 25
    assert cfg.issues_daily_write_cap == 100
    assert_ready(cfg)


def test_issue_filing_defaults_are_inert(tmp_path):
    cfg = load_config(write(tmp_path, VALID))

    assert cfg.issues_enabled is False
    assert cfg.issues_environments == ()
    assert cfg.issues_proxy_url == ""
    assert cfg.issues_conventions == ()
    assert cfg.issues_issue_types == ()
    assert cfg.issues_fields == ()
    assert cfg.issues_project_number == 0
    assert cfg.issues_daily_create_cap == 25
    assert cfg.issues_daily_write_cap == 100
    assert not cfg.issues_enabled_for("staging")


def test_issue_filing_is_on_only_for_the_listed_environments(tmp_path):
    cfg = load_config(write(tmp_path, FILING))

    assert cfg.issues_enabled_for("staging")
    assert not cfg.issues_enabled_for("prod")


def test_an_empty_issue_environment_list_means_every_environment(tmp_path):
    body = FILING.replace("environments: [staging]", "environments: []")
    assert body != FILING

    cfg = load_config(write(tmp_path, body))

    assert cfg.issues_enabled_for("staging")
    assert cfg.issues_enabled_for("prod")


def test_issue_filing_off_is_off_for_every_environment(tmp_path):
    body = FILING.replace("  enabled: true\n  environments", "  enabled: false\n  environments")
    assert body != FILING

    cfg = load_config(write(tmp_path, body))

    assert not cfg.issues_enabled_for("staging")
    assert not cfg.issues_enabled_for("prod")


@pytest.mark.parametrize(
    ("old", "new", "rule"),
    [
        ('proxy_url: "https://example.test/issues"', 'proxy_url: ""', "issues.proxy_url is required"),
        (
            'proxy_url: "https://example.test/issues"',
            'proxy_url: "REPLACE_WITH_FUNCTION_URL/issues"',
            "issues.proxy_url is still a placeholder",
        ),
        (
            'proxy_url: "https://example.test/issues"',
            'proxy_url: "https://elsewhere.test/issues"',
            "share investigation.findings_url's origin",
        ),
        (
            'proxy_url: "https://example.test/issues"',
            'proxy_url: "http://example.test/issues"',
            "share investigation.findings_url's origin",
        ),
        ("conventions: [docs/ISSUES.md]", "conventions: []", "issues.conventions is required"),
        (
            "conventions: [docs/ISSUES.md]",
            "conventions: [REPLACE_WITH_CONVENTIONS_FILE]",
            "issues.conventions entry",
        ),
        ("environments: [staging]", "environments: [dev]", "not in environments"),
        ("issue_types: [Bug, Task]", 'issue_types: [Bug, ""]', "issues.issue_types"),
        ("issue_types: [Bug, Task]", "issue_types: [Bug, 7]", "issues.issue_types"),
        ("fields: [Priority, Effort]", 'fields: [Priority, "  "]', "issues.fields"),
        ("project_number: 3", "project_number: -1", "issues.project_number"),
        ("project_number: 3", 'project_number: "3"', "issues.project_number"),
        ("project_number: 3", "project_number: true", "issues.project_number"),
        ("daily_create_cap: 25", "daily_create_cap: 0", "issues.daily_create_cap"),
        ("daily_write_cap: 100", 'daily_write_cap: "100"', "issues.daily_write_cap"),
    ],
)
def test_enabled_issue_filing_refuses_each_unusable_value(tmp_path, old, new, rule):
    body = FILING.replace(old, new)
    assert body != FILING, "the substitution never fired"

    with pytest.raises(ConfigError, match=rule):
        assert_ready(load_config(write(tmp_path, body)))


def test_enabled_issue_filing_requires_the_app_id(tmp_path):
    """Issue writes are made as the autofix App, even with autofix off."""
    with pytest.raises(ConfigError, match="autofix.app_id is required when issues is enabled"):
        assert_ready(load_config(write(tmp_path, VALID + ISSUES)))


def test_the_proxy_origin_match_ignores_host_case(tmp_path):
    body = FILING.replace(
        'proxy_url: "https://example.test/issues"', 'proxy_url: "https://EXAMPLE.test/issues"'
    )
    assert body != FILING

    assert_ready(load_config(write(tmp_path, body)))


def test_disabled_issue_filing_is_not_checked(tmp_path):
    """The example ships every issues value empty or a placeholder, and must
    still come up: nothing is checked until filing is turned on."""
    body = VALID + textwrap.dedent(
        """
        issues:
          enabled: false
          proxy_url: "REPLACE_WITH_FUNCTION_URL/issues"
          project_number: -1
        """
    )

    assert_ready(load_config(write(tmp_path, body)))


def test_the_example_issues_block_loads_to_the_inert_defaults():
    """Key for key the ReceiverConfig defaults, so the shipped template
    files nothing until an operator turns it on."""
    from receiver.config import EXAMPLE_CONFIG_PATH, ReceiverConfig

    cfg = load_config(EXAMPLE_CONFIG_PATH)

    for name in (
        "issues_enabled",
        "issues_environments",
        "issues_conventions",
        "issues_issue_types",
        "issues_fields",
        "issues_project_number",
        "issues_daily_create_cap",
        "issues_daily_write_cap",
    ):
        assert getattr(cfg, name) == ReceiverConfig.__dataclass_fields__[name].default, name
    assert cfg.issues_proxy_url == "REPLACE_WITH_FUNCTION_URL/issues"
    assert "issues:" in EXAMPLE_CONFIG_PATH.read_text()


def test_the_example_issues_block_adds_no_startup_refusal():
    """The template's other placeholders still need filling, so they are
    filled here; what is left is exactly what the issues block contributes,
    and it must pass as shipped."""
    import dataclasses

    from receiver.config import EXAMPLE_CONFIG_PATH

    cfg = dataclasses.replace(
        load_config(EXAMPLE_CONFIG_PATH),
        tenant_id="tenant-123",
        bot_app_id="app-456",
        channels={"prod": "19:prod@thread.tacv2", "staging": "19:staging@thread.tacv2"},
    )

    assert_ready(cfg)
