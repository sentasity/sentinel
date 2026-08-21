#!/usr/bin/env python3
"""CDK entry point. Account and region come from the engine config, never code."""

import sys
from pathlib import Path

import aws_cdk as cdk

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from infra.stacks.receiver_stack import ReceiverStack  # noqa: E402
from receiver.config import assert_ready, load_config  # noqa: E402

cfg = load_config()
assert_ready(cfg)

app = cdk.App()
ReceiverStack(
    app,
    "sentinel-receiver",
    cfg=cfg,
    env=cdk.Environment(account=cfg.account, region=cfg.region),
)
app.synth()
