"""Shared pytest fixtures and test data builders."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from airvpn_picker.api import Server

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def status_sample() -> dict[str, Any]:
    """Real AirVPN /api/status response captured from the live API."""
    return json.loads((FIXTURES / "status_sample.json").read_text())


def make_server(
    *,
    name: str = "Test",
    country: str = "de",
    continent: str = "Europe",
    location: str = "Frankfurt",
    health: str = "ok",
    load: int = 30,
    users: int = 100,
    users_max: int = 1000,
    bw: int = 100,
    bw_max: int = 1000,
    scorebase: int = 0,
    ips: tuple[str, ...] = ("1.2.3.4",),
) -> Server:
    """Build a `Server` for tests with sensible defaults.

    Every field has a default so tests only override what they care about.
    Keyword-only to keep call sites self-documenting.
    """
    return Server(
        public_name=name,
        country_code=country,
        country_name=country.upper(),
        continent=continent,
        location=location,
        health=health,
        currentload=load,
        users=users,
        users_max=users_max,
        bw=bw,
        bw_max=bw_max,
        scorebase=scorebase,
        ips_v4=ips,
    )


def constant_ping(_ip: str) -> float:
    """Stand-in ping_lookup that returns 50ms for every IP."""
    return 50.0
