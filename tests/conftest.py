"""Shared pytest fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def status_sample() -> dict[str, Any]:
    """Real AirVPN /api/status response captured from the live API."""
    return json.loads((FIXTURES / "status_sample.json").read_text())
