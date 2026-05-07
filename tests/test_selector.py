"""Tests for the selection algorithm and hysteresis logic."""

from __future__ import annotations

from typing import Any

import pytest

from airvpn_picker.api import parse_status
from airvpn_picker.selector import (
    Decision,
    NoCandidatesError,
    SelectorOptions,
    decide,
    filter_candidates,
)
from tests.conftest import constant_ping, make_server


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
            decide(
                servers=[make_server()],
                current_endpoint_ip=None,
                options=opts,
                ping_lookup=constant_ping,
            )

    def test_picks_lowest_score(self) -> None:
        # With constant 50ms ping and default weights:
        #   score = ping + load + users_pct  (default users_max=1000, users=100 -> 10%)
        #   High score = 50 + 60 + 10 = 120
        #   Low  score = 50 + 10 + 10 =  70
        #   Mid  score = 50 + 30 + 10 =  90
        servers = [
            make_server(name="High", load=60, ips=("10.0.0.1",)),
            make_server(name="Low", load=10, ips=("10.0.0.2",)),
            make_server(name="Mid", load=30, ips=("10.0.0.3",)),
        ]
        result = decide(
            servers=servers,
            current_endpoint_ip=None,
            options=SelectorOptions(),
            ping_lookup=constant_ping,
        )
        assert result.action == "switch"
        assert result.winner.public_name == "Low"
        assert result.endpoint_ip == "10.0.0.2"

    def test_ping_dominates_when_all_else_equal(self) -> None:
        servers = [
            make_server(name="Far", load=10, ips=("10.0.0.1",)),
            make_server(name="Near", load=10, ips=("10.0.0.2",)),
        ]
        # Far is 200ms, Near is 20ms — Near should win even though loads tie.
        pings = {"10.0.0.1": 200.0, "10.0.0.2": 20.0}
        result = decide(
            servers=servers,
            current_endpoint_ip=None,
            options=SelectorOptions(),
            ping_lookup=lambda ip: pings.get(ip, -1),
        )
        assert result.winner.public_name == "Near"

    def test_noop_when_current_matches_winner(self) -> None:
        winner = make_server(name="Best", load=10, ips=("10.0.0.1", "10.0.0.2"))
        result = decide(
            servers=[winner],
            current_endpoint_ip="10.0.0.2",  # any of winner's IPs counts as a match
            options=SelectorOptions(),
            ping_lookup=constant_ping,
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
            ping_lookup=constant_ping,
        )
        assert result.action == "switch"
        assert result.reason == "current-unhealthy"

    def test_force_switch_when_current_is_in_disallowed_geo(self) -> None:
        # Current endpoint is a healthy, low-load server — but in a country the
        # operator has explicitly excluded via --allowed-countries. Hysteresis
        # must NOT apply here: the operator wants to leave this geo entirely.
        # Regression test for a bug observed in live testing where a Dutch
        # server was kept despite the allowlist being {de}.
        servers = [
            make_server(name="NLServer", country="nl", load=38, ips=("10.0.0.1",)),
            make_server(name="DEServer", country="de", load=31, ips=("10.0.0.2",)),
        ]
        result = decide(
            servers=servers,
            current_endpoint_ip="10.0.0.1",
            options=SelectorOptions(allowed_countries=("de",), hysteresis_score=15),
            ping_lookup=constant_ping,
        )
        # NL is filtered out by geo allowlist -> current-unhealthy, no hysteresis.
        assert result.action == "switch"
        assert result.reason == "current-unhealthy"
        assert result.winner.public_name == "DEServer"

    def test_force_switch_when_current_is_in_disallowed_continent(self) -> None:
        # Same bug, continent variant.
        servers = [
            make_server(name="USServer", continent="America", load=10, ips=("10.0.0.1",)),
            make_server(name="DEServer", continent="Europe", load=40, ips=("10.0.0.2",)),
        ]
        result = decide(
            servers=servers,
            current_endpoint_ip="10.0.0.1",
            options=SelectorOptions(allowed_continents=("Europe",), hysteresis_score=15),
            ping_lookup=constant_ping,
        )
        assert result.action == "switch"
        assert result.reason == "current-unhealthy"
        assert result.winner.public_name == "DEServer"

    def test_hysteresis_blocks_small_improvement(self) -> None:
        # score(constant ping=50, users_pct=10): current=50+50+10=110, better=50+40+10=100
        # delta=10 < 15 -> noop
        servers = [
            make_server(name="Current", load=50, ips=("10.0.0.1",)),
            make_server(name="Slightly Better", load=40, ips=("10.0.0.2",)),
        ]
        result = decide(
            servers=servers,
            current_endpoint_ip="10.0.0.1",
            options=SelectorOptions(hysteresis_score=15),
            ping_lookup=constant_ping,
        )
        assert result.action == "noop"
        assert result.reason == "below-hysteresis"

    def test_hysteresis_allows_meaningful_improvement(self) -> None:
        # current=50+60+10=120, better=50+20+10=80, delta=40 > 15 -> switch
        servers = [
            make_server(name="Current", load=60, ips=("10.0.0.1",)),
            make_server(name="Much Better", load=20, ips=("10.0.0.2",)),
        ]
        result = decide(
            servers=servers,
            current_endpoint_ip="10.0.0.1",
            options=SelectorOptions(hysteresis_score=15),
            ping_lookup=constant_ping,
        )
        assert result.action == "switch"
        assert result.reason == "score-improvement"
        assert result.winner.public_name == "Much Better"

    def test_penalty_pushes_a_server_down_the_ranking(self) -> None:
        # Apple looks best on raw load but has 1 penalty; with default
        # penalty_factor=1000, Apple's score gets +1000 and Banana wins.
        servers = [
            make_server(name="Apple", load=10, ips=("10.0.0.1",)),
            make_server(name="Banana", load=30, ips=("10.0.0.2",)),
        ]
        penalties = {"10.0.0.1": 1}
        result = decide(
            servers=servers,
            current_endpoint_ip=None,
            options=SelectorOptions(),
            ping_lookup=constant_ping,
            penalty_lookup=lambda ip: penalties.get(ip, 0),
        )
        assert result.winner.public_name == "Banana"

    def test_decision_includes_candidate_count(self) -> None:
        servers = [
            make_server(name="A", load=10),
            make_server(name="B", load=20),
            make_server(name="C", load=30),
            make_server(name="D", continent="America"),  # filtered out
        ]
        result = decide(
            servers=servers,
            current_endpoint_ip=None,
            options=SelectorOptions(),
            ping_lookup=constant_ping,
        )
        assert result.candidates_count == 3


class TestDecideWithRealFixture:
    def test_picks_a_european_winner(self, status_sample: dict[str, Any]) -> None:
        servers = parse_status(status_sample)
        result = decide(
            servers=servers,
            current_endpoint_ip=None,
            options=SelectorOptions(allowed_continents=("Europe",), max_load=80),
            ping_lookup=constant_ping,
        )
        assert isinstance(result, Decision)
        assert result.action == "switch"
        assert result.winner.continent == "Europe"
        assert result.winner.health == "ok"
        assert result.winner.currentload <= 80
