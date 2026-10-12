"""Fixture loaders shared across the receiver's test modules."""

import json
import os
from pathlib import Path

import pytest

# The receiver initialises the Sentry SDK at import from SENTRY_DSN, and the
# SDK reads its other SENTRY_* settings from the environment as well. Drop all
# of them before any test module imports the receiver, so a shell that exports
# a DSN for some project never has the suite report into it.
for _name in [name for name in os.environ if name.startswith("SENTRY_")]:
    del os.environ[_name]

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load_fixture(name: str):
    """Return the parsed JSON fixture at `fixtures/<name>`."""
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES
