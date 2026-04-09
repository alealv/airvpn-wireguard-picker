"""Tests for the selection algorithm and hysteresis logic."""

from __future__ import annotations

from typing import Any

import pytest

from airvpn_picker.api import Server, parse_status
from airvpn_picker.selector import (
    Decision,
    NoCandidatesError,
    SelectorOptions,
    decide,
    filter_candidates,
)


def make_server(
    name: str = "Test",
    country: str = "de",
    continent: str = "Europe",
    health: str = "ok",
    load: int = 30,
    users: int = 100,
    ips: tuple[str, ...] = ("1.2.3.4",),
) -> Server:
    return Server(
        public_name=name,
        country_code=country,
        country_name=country.upper(),
        continent=continent,
        location="Frankfurt",
        health=health,
        currentload=load,
        users=users,
        bw=100,
        bw_max=1000,
        ips_v4=ips,
    )


class TestFilterCandidates:
    def test_filters_by_continent(self) -> None:
        servers = [
            make_server(name="A", continent="Europe"),
            make_server(name="B", continent="America"),
        ]
        opts = SelectorOptions(allowed_continents=("Europe",))
        assert [s.public_name for s in filter_candidates(servers, opts)] == ["A"]

    def test_filters_by_country_overrides_continent(self) -> None:
        servers = [
            make_server(name="DE", country="de", continent="Europe"),
            make_server(name="NL", country="nl", continent="Europe"),
            make_server(name="FR", country="fr", continent="Europe"),
        ]
        opts = SelectorOptions(allowed_countries=("de", "fr"))
        names = [s.public_name for s in filter_candidates(servers, opts)]
        assert names == ["DE", "FR"]

    def test_filters_unhealthy(self) -> None:
        servers = [
            make_server(name="OK", health="ok"),
            make_server(name="WARN", health="warning"),
            make_server(name="ERR", health="error"),
        ]
        opts = SelectorOptions()
        names = [s.public_name for s in filter_candidates(servers, opts)]
        assert names == ["OK"]

    def test_filters_overloaded(self) -> None:
        servers = [
            make_server(name="Light", load=20),
            make_server(name="Heavy", load=85),
        ]
        opts = SelectorOptions(max_load=80)
        names = [s.public_name for s in filter_candidates(servers, opts)]
        assert names == ["Light"]

    def test_country_codes_are_case_insensitive(self) -> None:
        servers = [make_server(country="de")]
        opts = SelectorOptions(allowed_countries=("DE",))
        assert filter_candidates(servers, opts)


class TestDecide:
    def test_raises_when_no_candidates(self) -> None:
        opts = SelectorOptions(allowed_countries=("xx",))
        with pytest.raises(NoCandidatesError):
            decide(servers=[make_server()], current_endpoint_ip=None, options=opts)

    def test_picks_lowest_load(self) -> None:
        servers = [
            make_server(name="High", load=60, ips=("10.0.0.1",)),
            make_server(name="Low", load=10, ips=("10.0.0.2",)),
            make_server(name="Mid", load=30, ips=("10.0.0.3",)),
        ]
        result = decide(servers=servers, current_endpoint_ip=None, options=SelectorOptions())
        assert result.action == "switch"
        assert result.winner.public_name == "Low"
        assert result.endpoint_ip == "10.0.0.2"

    def test_tiebreaks_by_users_then_bw(self) -> None:
        servers = [
            make_server(name="A", load=10, users=200, ips=("10.0.0.1",)),
            make_server(name="B", load=10, users=100, ips=("10.0.0.2",)),
            make_server(name="C", load=10, users=100, ips=("10.0.0.3",)),
        ]
        servers[2] = Server(
            public_name="C",
            country_code="de",
            country_name="DE",
            continent="Europe",
            location="Frankfurt",
            health="ok",
            currentload=10,
            users=100,
            bw=50,  # lower than B's 100
            bw_max=1000,
            ips_v4=("10.0.0.3",),
        )
        result = decide(servers=servers, current_endpoint_ip=None, options=SelectorOptions())
        assert result.winner.public_name == "C"

    def test_noop_when_current_matches_winner(self) -> None:
        winner = make_server(name="Best", load=10, ips=("10.0.0.1", "10.0.0.2"))
        result = decide(
            servers=[winner],
            current_endpoint_ip="10.0.0.2",  # any of winner's IPs counts as a match
            options=SelectorOptions(),
        )
        assert result.action == "noop"
        assert result.reason == "already-on-winner"

    def test_force_switch_when_current_unhealthy(self) -> None:
        # Current endpoint is not in the candidate set at all (unhealthy/unknown).
        servers = [
            make_server(name="Best", load=50, ips=("10.0.0.1",)),
        ]
        result = decide(
            servers=servers,
            current_endpoint_ip="9.9.9.9",  # unknown to AirVPN -> treat as unhealthy
            options=SelectorOptions(),
        )
        assert result.action == "switch"
        assert result.reason == "current-unhealthy"

    def test_hysteresis_blocks_small_improvement(self) -> None:
        servers = [
            make_server(name="Current", load=50, ips=("10.0.0.1",)),
            make_server(name="Slightly Better", load=40, ips=("10.0.0.2",)),
        ]
        result = decide(
            servers=servers,
            current_endpoint_ip="10.0.0.1",
            options=SelectorOptions(hysteresis_pp=15),
        )
        # delta is 10pp, below the 15pp threshold
        assert result.action == "noop"
        assert result.reason == "below-hysteresis"

    def test_hysteresis_allows_meaningful_improvement(self) -> None:
        servers = [
            make_server(name="Current", load=60, ips=("10.0.0.1",)),
            make_server(name="Much Better", load=20, ips=("10.0.0.2",)),
        ]
        result = decide(
            servers=servers,
            current_endpoint_ip="10.0.0.1",
            options=SelectorOptions(hysteresis_pp=15),
        )
        assert result.action == "switch"
        assert result.reason == "load-improvement"
        assert result.winner.public_name == "Much Better"

    def test_decision_includes_candidate_count(self) -> None:
        servers = [
            make_server(name="A", load=10),
            make_server(name="B", load=20),
            make_server(name="C", load=30),
            make_server(name="D", continent="America"),  # filtered out
        ]
        result = decide(servers=servers, current_endpoint_ip=None, options=SelectorOptions())
        assert result.candidates_count == 3


class TestDecideWithRealFixture:
    def test_picks_a_european_winner(self, status_sample: dict[str, Any]) -> None:
        servers = parse_status(status_sample)
        result = decide(
            servers=servers,
            current_endpoint_ip=None,
            options=SelectorOptions(allowed_continents=("Europe",), max_load=80),
        )
        assert isinstance(result, Decision)
        assert result.action == "switch"
        assert result.winner.continent == "Europe"
        assert result.winner.health == "ok"
        assert result.winner.currentload <= 80
