"""Setup runbooks must name exactly the SSM parameters the receiver reads.

The runbooks live on the documentation site; the repo files are pointers to it.
"""

import re
from pathlib import Path

from receiver.config import SECRET_KEYS

DOCS = Path(__file__).resolve().parent.parent / "website" / "src" / "content" / "docs"
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
PARAM = re.compile(r"/sentinel/([a-z0-9-]+)(?![a-z0-9\-/])")


def named_parameters(filename: str) -> set[str]:
    return set(PARAM.findall((DOCS / filename).read_text()))


def test_microsoft_runbook_names_only_real_parameters():
    assert named_parameters("deploy/microsoft.mdx") <= set(SECRET_KEYS)


def test_microsoft_runbook_covers_the_bot_client_secret():
    assert "bot-client-secret" in named_parameters("deploy/microsoft.mdx")


def test_sentry_runbook_names_only_real_parameters():
    assert named_parameters("deploy/sentry.mdx") <= set(SECRET_KEYS)


def test_sentry_runbook_covers_both_sentry_parameters():
    named = named_parameters("deploy/sentry.mdx")

    assert {"sentry-webhook-secret", "sentry-api-token"} <= named


def test_put_parameters_writes_every_secret_the_receiver_reads():
    """A key in SECRET_KEYS with no `put` line is a secret nobody can set."""
    body = (SCRIPTS / "put-parameters.sh").read_text()

    for key in SECRET_KEYS:
        assert f"put {key} " in body, f"put-parameters.sh never writes {key}"
