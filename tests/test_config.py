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
      sentry_org: sentasity
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
      repo: sentasity/sentinel
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
    assert cfg.autofix_repo == "sentasity/sentinel"
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


def test_enabled_autofix_requires_app_id_repo_and_callback(tmp_path):
    body = VALID + AUTOFIX.replace('app_id: "1234567"', 'app_id: ""')

    with pytest.raises(ConfigError, match="autofix.app_id"):
        assert_ready(load_config(write(tmp_path, body)))


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
