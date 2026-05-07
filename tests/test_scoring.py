"""Tests for Eddie-compatible scoring."""

from __future__ import annotations

from airvpn_picker.scoring import UNKNOWN_PING_PENALTY_MS, ScoreWeights, score


class TestScoreSpeedMode:
    def test_baseline_components_sum(self) -> None:
        # Default factors are 1; in speed mode no divisors apply.
        s = score(ping_ms=20, load_pct=30, users_pct=10, scorebase=5, penalty=0)
        assert s == 20 + 30 + 10 + 5

    def test_penalty_dominates_with_default_factor_1000(self) -> None:
        clean = score(ping_ms=20, load_pct=30, users_pct=10)
        punished = score(ping_ms=20, load_pct=30, users_pct=10, penalty=1)
        assert punished == clean + 1000

    def test_unknown_ping_treated_as_high_penalty(self) -> None:
        unknown = score(ping_ms=-1, load_pct=10, users_pct=0)
        known_fast = score(ping_ms=10, load_pct=10, users_pct=0)
        assert unknown == UNKNOWN_PING_PENALTY_MS + 10
        assert unknown > known_fast


class TestScoreLatencyMode:
    def test_load_and_users_get_divided_down(self) -> None:
        weights = ScoreWeights(mode="latency")
        # ping is undivided; load divided by 10; users divided by 10
        s = score(ping_ms=20, load_pct=50, users_pct=20, scorebase=500, weights=weights)
        # 20 + 50/10 + 20/10 + 500/500 = 20 + 5 + 2 + 1 = 28
        assert s == 28

    def test_ping_dominates_in_latency_mode(self) -> None:
        weights = ScoreWeights(mode="latency")
        far = score(ping_ms=200, load_pct=10, users_pct=0, weights=weights)
        near = score(ping_ms=20, load_pct=80, users_pct=0, weights=weights)
        assert near < far  # ping dominates load even though far has lower load


class TestCustomWeights:
    def test_can_zero_out_a_dimension(self) -> None:
        # If users_factor=0 the users_pct component disappears.
        weights = ScoreWeights(users_factor=0.0)
        with_users = score(ping_ms=10, load_pct=10, users_pct=50)
        without_users = score(ping_ms=10, load_pct=10, users_pct=50, weights=weights)
        assert with_users == 10 + 10 + 50
        assert without_users == 10 + 10
