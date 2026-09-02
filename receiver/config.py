"""Receiver configuration: one YAML file plus SSM SecureStrings."""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from pathlib import Path

import boto3
import yaml

# The deployment's own config file, deliberately untracked: this repo ships
# only the `.example` template beside it. `RECEIVER_CONFIG` overrides the
# path for the Lambda (infra/stacks/receiver_stack.py) and for tests.
DEFAULT_CONFIG_FILENAME = "receiver.yaml"
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
DEFAULT_CONFIG_PATH = CONFIG_DIR / DEFAULT_CONFIG_FILENAME
EXAMPLE_CONFIG_PATH = CONFIG_DIR / f"{DEFAULT_CONFIG_FILENAME}.example"
PLACEHOLDER_PREFIX = "REPLACE_WITH_"

# Every SSM SecureString the receiver reads. `scripts/put-parameters.sh` writes
# exactly these, and the setup runbooks are tested against this tuple so docs
# and code cannot drift apart.
SECRET_KEYS = (
    "sentry-webhook-secret",
    "sentry-api-token",
    "bot-client-secret",
    "routine-trigger-token",
    "github-app-private-key",
)

# The only environments whose events carry a resolvable release. Deliberately a
# constant rather than a config knob: the whole investigation pipeline checks
# out the event's release SHA, and a per-developer environment has none.
#
# The usual cause is a dev deploy path that leaves SENTRY_RELEASE unset on
# purpose, because stamping it would force a full infrastructure update on every
# commit. Those events arrive with `release=""` while CI sets it for prod and
# staging, so the two environments look the same until the gate reads the
# release.
#
# Adding a dev environment here would not degrade gracefully. It would check
# out nothing, or worse, a stale SHA that happens to exist, and investigate the
# wrong code confidently.
INVESTIGABLE_ENVIRONMENTS = ("prod", "staging")

# `shadow` runs the gate and records what it would have fired without firing;
# `on-request` is reserved for a human-initiated investigation and fires
# nothing automatically. Only the modes in FIRING_MODES need a routine.
TRIGGER_MODES = ("auto", "shadow", "on-request")
FIRING_MODES = ("auto",)

# Ranked levels shared by the two autofix gate signals. Mirrors
# findings.CONFIDENCES; kept local so config stays import-independent.
AUTOFIX_LEVELS = ("high", "medium", "low")

# One member on purpose. A full event body is not a supported mode: measured
# 2026-08-13, live frontend events reached 190KB, 217KB, and 304KB against the
# fire endpoint's 65,536-character cap, so `full-event` fails on real traffic
# rather than occasionally.
PAYLOAD_MODES = ("issue-ids-only",)


class ConfigError(RuntimeError):
    """Configuration is missing, incomplete, or still carrying placeholders."""


@dataclass(frozen=True)
class ReceiverConfig:
    """Everything project-specific the receiver needs, loaded once per container."""

    environments: tuple[str, ...]
    account: str
    region: str
    table_name: str
    alarm_email: str
    tenant_id: str
    service_url: str
    bot_app_id: str
    channels: dict[str, str]
    sentry_org: str
    ssm_prefix: str
    automation_dsn: str

    # Investigation engine. These carry defaults because the fields
    # above do not, and a dataclass requires defaulted fields last. The
    # defaults are also what makes an incomplete config inert: `trigger_mode`
    # starts at the one mode that never fires.
    target_repo: str = ""
    release_to_sha: str = "identity"
    trigger_mode: str = "shadow"
    payload_mode: str = "issue-ids-only"
    routine_id: str = ""
    # The probe is a separate routine with its own prompt and its own trigger
    # token. Optional: the receiver never fires it, so an empty value costs
    # only the probe runbook.
    probe_routine_id: str = ""
    probe_token_ref: str = "probe-trigger-token"
    findings_url: str = ""
    debounce_seconds: int = 60
    max_batch_issues: int = 8
    daily_fire_cap: int = 40
    per_sweep_fire_cap: int = 4
    deadline_seconds: int = 900

    # Autofix gate. The enabled False default keeps the gate closed;
    # an empty project allowlist opts in every project.
    autofix_enabled: bool = False
    autofix_projects: tuple[str, ...] = ()
    autofix_min_confidence: str = "high"
    autofix_min_fixability: str = "high"
    autofix_exclude_paths: tuple[str, ...] = ()
    autofix_daily_pr_cap: int = 5
    autofix_app_id: str = ""
    autofix_base_branch: str = ""
    autofix_callback_url: str = ""

    def secret_name(self, key: str) -> str:
        """Return the full SSM parameter name for a secret key."""
        return f"{self.ssm_prefix}/{key}"

    @property
    def release_to_sha_is_identity(self) -> bool:
        """True when a Sentry release value IS the git commit SHA."""
        return self.release_to_sha == "identity"


def load_config(path: str | Path | None = None) -> ReceiverConfig:
    """Parse the receiver's YAML config. Does not touch AWS."""
    resolved = Path(path or os.environ.get("RECEIVER_CONFIG") or DEFAULT_CONFIG_PATH)
    if not resolved.is_file():
        raise ConfigError(f"config file not found: {resolved}")

    raw = yaml.safe_load(resolved.read_text()) or {}
    aws = raw.get("aws") or {}
    teams = raw.get("teams") or {}
    webhook = raw.get("webhook") or {}
    observability = raw.get("observability") or {}
    trigger = raw.get("trigger") or {}
    investigation = raw.get("investigation") or {}
    autofix = raw.get("autofix") or {}

    return ReceiverConfig(
        environments=tuple(raw.get("environments") or ()),
        account=str(aws.get("account") or ""),
        region=aws.get("region") or "us-east-1",
        table_name=aws.get("table_name") or "",
        alarm_email=aws.get("alarm_email") or "",
        tenant_id=teams.get("tenant_id") or "",
        service_url=teams.get("service_url") or "",
        bot_app_id=teams.get("bot_app_id") or "",
        channels=dict(teams.get("channels") or {}),
        sentry_org=webhook.get("sentry_org") or "",
        ssm_prefix=webhook.get("ssm_prefix") or "",
        automation_dsn=observability.get("automation_dsn") or "",
        target_repo=raw.get("target_repo") or "",
        release_to_sha=raw.get("release_to_sha") or "identity",
        trigger_mode=raw.get("trigger_mode") or "shadow",
        payload_mode=raw.get("payload_mode") or "issue-ids-only",
        routine_id=str(trigger.get("routine_id") or ""),
        probe_routine_id=str(trigger.get("probe_routine_id") or ""),
        probe_token_ref=str(trigger.get("probe_token_ref") or "probe-trigger-token"),
        findings_url=investigation.get("findings_url") or "",
        debounce_seconds=int(investigation.get("debounce_seconds") or 60),
        max_batch_issues=int(investigation.get("max_batch_issues") or 8),
        daily_fire_cap=int(investigation.get("daily_fire_cap") or 40),
        per_sweep_fire_cap=int(investigation.get("per_sweep_fire_cap") or 4),
        deadline_seconds=int(investigation.get("deadline_seconds") or 900),
        autofix_enabled=bool(autofix.get("enabled") or False),
        autofix_projects=tuple(autofix.get("projects") or ()),
        autofix_min_confidence=autofix.get("min_confidence") or "high",
        autofix_min_fixability=autofix.get("min_fixability") or "high",
        autofix_exclude_paths=tuple(autofix.get("exclude_paths") or ()),
        autofix_daily_pr_cap=int(autofix.get("daily_pr_cap") or 5),
        autofix_app_id=str(autofix.get("app_id") or ""),
        autofix_base_branch=autofix.get("base_branch") or "",
        autofix_callback_url=autofix.get("callback_url") or "",
    )


def assert_ready(cfg: ReceiverConfig) -> None:
    """Fail closed at cold start when the config cannot serve a request.

    Raises ConfigError rather than letting the receiver come up half-wired and
    drop alerts at request time.
    """
    if not cfg.environments:
        raise ConfigError("no environments configured")

    unexpected = [e for e in cfg.environments if e not in INVESTIGABLE_ENVIRONMENTS]
    if unexpected:
        raise ConfigError(
            f"environment(s) {', '.join(sorted(unexpected))} are not investigable. "
            f"Only {', '.join(INVESTIGABLE_ENVIRONMENTS)} carry a release SHA; a "
            f"per-developer environment has none, so release-checkout would fail "
            f"on every event. See INVESTIGABLE_ENVIRONMENTS."
        )

    if cfg.trigger_mode not in TRIGGER_MODES:
        raise ConfigError(
            f"trigger_mode {cfg.trigger_mode!r} is not one of {', '.join(TRIGGER_MODES)}"
        )

    if cfg.payload_mode not in PAYLOAD_MODES:
        raise ConfigError(
            f"payload_mode {cfg.payload_mode!r} is not supported. Only "
            f"{', '.join(PAYLOAD_MODES)} exists: a full event body can exceed the "
            f"fire endpoint's 65,536-character cap on real traffic."
        )

    if not cfg.target_repo:
        raise ConfigError("target_repo is not set")

    if cfg.trigger_mode in FIRING_MODES:
        for name, value in (
            ("routine_id", cfg.routine_id),
            ("findings_url", cfg.findings_url),
        ):
            if not value:
                raise ConfigError(
                    f"{name} is required when trigger_mode is {cfg.trigger_mode!r}"
                )
            if value.startswith(PLACEHOLDER_PREFIX):
                raise ConfigError(f"{name} is still a placeholder: {value}")

    for name, value in (
        ("table_name", cfg.table_name),
        ("tenant_id", cfg.tenant_id),
        ("service_url", cfg.service_url),
        ("bot_app_id", cfg.bot_app_id),
        ("ssm_prefix", cfg.ssm_prefix),
    ):
        if not value:
            raise ConfigError(f"{name} is not set")
        if value.startswith(PLACEHOLDER_PREFIX):
            raise ConfigError(f"{name} is still a placeholder: {value}")

    for name, value in (
        ("autofix.min_confidence", cfg.autofix_min_confidence),
        ("autofix.min_fixability", cfg.autofix_min_fixability),
    ):
        if value not in AUTOFIX_LEVELS:
            raise ConfigError(
                f"{name} {value!r} is not one of {', '.join(AUTOFIX_LEVELS)}"
            )

    if cfg.autofix_enabled:
        for name, value in (
            ("autofix.app_id", cfg.autofix_app_id),
            ("autofix.base_branch", cfg.autofix_base_branch),
            ("autofix.callback_url", cfg.autofix_callback_url),
        ):
            if not value:
                raise ConfigError(f"{name} is required when autofix is enabled")
            if value.startswith(PLACEHOLDER_PREFIX):
                raise ConfigError(f"{name} is still a placeholder: {value}")

    for environment in cfg.environments:
        channel = cfg.channels.get(environment)
        if not channel:
            raise ConfigError(f"environment {environment!r} has no Teams channel mapping")
        if channel.startswith(PLACEHOLDER_PREFIX):
            raise ConfigError(f"environment {environment!r} channel is still a placeholder")


@functools.lru_cache(maxsize=8)
def get_secret(name: str) -> str:
    """Read a decrypted SSM SecureString, cached for the container's lifetime."""
    client = boto3.client("ssm")
    return client.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
