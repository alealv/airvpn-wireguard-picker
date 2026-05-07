"""Eddie-compatible server scoring.

Mirrors the scoring formula used by AirVPN's official client (Eddie),
specifically `Lib.Core/ConnectionInfo.cs::Score()`:

    Score = PingB + LoadB + UsersB + ScoreBaseB + PenalityB

where each term may be scaled by a per-mode factor:

    speed   mode: factors are 1 for ScoreBase, Load, Users
    latency mode: ScoreBase / 500, Load / 10, Users / 10

Lower score = better server. Ping is the dominant term in latency mode
because it isn't divided down. Penalty is multiplied by 1000 by default
so a single recent failure pushes a server far down the ranking.

This module is pure (no I/O, no clock, no globals) so it's exhaustively
unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ScoreMode = Literal["speed", "latency"]

DEFAULT_PING_FACTOR = 1.0
DEFAULT_LOAD_FACTOR = 1.0
DEFAULT_USERS_FACTOR = 1.0
DEFAULT_PENALTY_FACTOR = 1000.0
DEFAULT_LATENCY_SCOREBASE_DIVISOR = 500.0
DEFAULT_LATENCY_LOAD_DIVISOR = 10.0
DEFAULT_LATENCY_USERS_DIVISOR = 10.0

# Pings can't go below 0; a missing measurement should not silently win
# the score. Treat a -1 / unknown ping as a high-penalty fallback so the
# picker prefers any server with a real reading.
UNKNOWN_PING_PENALTY_MS = 999.0


@dataclass(frozen=True, slots=True)
class ScoreWeights:
    """Tunable weights, defaults match Eddie's AirVPN provider config."""

    mode: ScoreMode = "speed"
    ping_factor: float = DEFAULT_PING_FACTOR
    load_factor: float = DEFAULT_LOAD_FACTOR
    users_factor: float = DEFAULT_USERS_FACTOR
    penalty_factor: float = DEFAULT_PENALTY_FACTOR
    latency_scorebase_divisor: float = DEFAULT_LATENCY_SCOREBASE_DIVISOR
    latency_load_divisor: float = DEFAULT_LATENCY_LOAD_DIVISOR
    latency_users_divisor: float = DEFAULT_LATENCY_USERS_DIVISOR


def score(
    *,
    ping_ms: float,
    load_pct: float,
    users_pct: float,
    scorebase: float = 0.0,
    penalty: int = 0,
    weights: ScoreWeights | None = None,
) -> float:
    """Compute Eddie's score for one server. Lower is better.

    Args:
        ping_ms: Median measured ICMP RTT in ms. Negative or NaN treated
            as ``UNKNOWN_PING_PENALTY_MS``.
        load_pct: Reported load percentage (0..100).
        users_pct: Reported users-of-cap percentage (0..100).
        scorebase: Static scorebase reported by the AirVPN API (0 if unknown).
        penalty: Number of recent failed-handshake events for this server.
        weights: Tunable factors; defaults to Eddie's AirVPN provider values.

    Returns:
        A float that callers should sort ascending.
    """
    w = weights or ScoreWeights()

    effective_ping = ping_ms if ping_ms is not None and ping_ms >= 0 else UNKNOWN_PING_PENALTY_MS

    ping_b = effective_ping * w.ping_factor
    load_b = load_pct * w.load_factor
    users_b = users_pct * w.users_factor
    scorebase_b = float(scorebase)
    penalty_b = penalty * w.penalty_factor

    if w.mode == "latency":
        scorebase_b /= w.latency_scorebase_divisor
        load_b /= w.latency_load_divisor
        users_b /= w.latency_users_divisor

    return ping_b + load_b + users_b + scorebase_b + penalty_b
