"""The test session never reports to a real Sentry project.

`receiver.handler` initialises the Sentry SDK at import from SENTRY_DSN, and
the SDK reads its other SENTRY_* settings from the environment too. A shell
that exports a DSN for some other project would otherwise send every error a
test captures to that project, so `tests/conftest.py` drops every SENTRY_*
variable before any test module imports the receiver.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Nothing listens on the discard port, so even a regression sends nowhere.
FAKE_DSN = "https://public@127.0.0.1:9/1"


def test_the_session_carries_no_sentry_setting():
    assert not [name for name in os.environ if name.startswith("SENTRY_")]


def test_a_shell_dsn_never_turns_the_sdk_on_under_the_tests():
    """A fresh interpreter whose environment carries a DSN imports the
    conftest and then the handler, the order pytest uses, and must come up
    with the SDK off."""
    probe = (
        "import tests.conftest, receiver.handler, sentry_sdk, sys; "
        "sys.exit(1 if sentry_sdk.get_client().is_active() else 0)"
    )
    ran = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env={**os.environ, "SENTRY_DSN": FAKE_DSN},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert ran.returncode == 0, ran.stderr or "the Sentry SDK came up active"
