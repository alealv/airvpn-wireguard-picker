"""Tests for state.py ping cache + penalty helpers (v0.2 additions)."""

from __future__ import annotations

from airvpn_picker.state import (
    PenaltyRecord,
    PingSample,
    cached_ping,
    decay_penalties,
    increment_penalty,
    merge_ping_cache,
    penalty_for,
    stale_ips,
)


class TestMergePingCache:
    def test_seeds_cache_on_first_observation(self) -> None:
        merged = merge_ping_cache({}, {"1.1.1.1": 25.0}, now=100.0)
        assert merged["1.1.1.1"] == PingSample(ping_ms=25.0, timestamp=100.0)

    def test_ewma_smooths_subsequent_readings(self) -> None:
        cache = {"1.1.1.1": PingSample(ping_ms=10.0, timestamp=0.0)}
        merged = merge_ping_cache(cache, {"1.1.1.1": 50.0}, now=100.0, alpha=0.5)
        # 0.5*50 + 0.5*10 = 30
        assert merged["1.1.1.1"].ping_ms == 30.0

    def test_unreachable_does_not_poison_cache(self) -> None:
        cache = {"1.1.1.1": PingSample(ping_ms=10.0, timestamp=0.0)}
        merged = merge_ping_cache(cache, {"1.1.1.1": -1.0}, now=100.0)
        assert merged["1.1.1.1"] == PingSample(ping_ms=10.0, timestamp=0.0)


class TestStaleIps:
    def test_returns_only_expired_entries(self) -> None:
        cache = {
            "fresh": PingSample(ping_ms=10.0, timestamp=900.0),
            "old": PingSample(ping_ms=10.0, timestamp=100.0),
        }
        # ttl=600, now=1000 -> "old" was last seen at t=100, age=900 > 600
        assert stale_ips(cache, now=1000.0, ttl_s=600.0) == {"old"}


class TestCachedPing:
    def test_returns_minus_one_for_missing(self) -> None:
        assert cached_ping({}, "1.2.3.4") == -1.0


class TestPenalty:
    def test_increment_creates_then_increments(self) -> None:
        p = increment_penalty({}, "1.1.1.1", now=10.0)
        assert p["1.1.1.1"] == PenaltyRecord(count=1, last_touched=10.0)
        p2 = increment_penalty(p, "1.1.1.1", now=20.0)
        assert p2["1.1.1.1"] == PenaltyRecord(count=2, last_touched=20.0)

    def test_decay_drops_old_entries(self) -> None:
        p = {
            "fresh": PenaltyRecord(count=1, last_touched=900.0),
            "old": PenaltyRecord(count=5, last_touched=100.0),
        }
        # decay_after=600, now=1000 -> "old" expires (age=900)
        decayed = decay_penalties(p, now=1000.0, decay_after_s=600.0)
        assert "fresh" in decayed
        assert "old" not in decayed

    def test_penalty_for_returns_zero_when_absent(self) -> None:
        assert penalty_for({}, "1.2.3.4") == 0
