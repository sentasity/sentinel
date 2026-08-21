"""Fixture loaders shared across the receiver's test modules."""

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load_fixture(name: str):
    """Return the parsed JSON fixture at `fixtures/<name>`."""
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES
