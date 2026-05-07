"""Filter candidate servers and decide whether to switch the WireGuard endpoint.

The decision logic is deliberately simple and pure (no I/O, no subprocess) so
that it can be exhaustively unit tested with synthetic and fixture-based inputs.

Scoring follows AirVPN's official Eddie client: see ``scoring.py``. The picker
sorts candidates by Eddie score (lower wins), and hysteresis is applied to the
*score delta*, not the load delta.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from airvpn_picker.api import Server
from airvpn_picker.scoring import ScoreWeights, score

DEFAULT_ALLOWED_CONTINENTS: tuple[str, ...] = ("Europe",)
DEFAULT_MAX_LOAD = 80
# Score-space hysteresis. Eddie scores typically land in the 10-200 range
# under default weights with realistic AirVPN data, so a 15-point gap means
# "winner is meaningfully better, not flapping".
DEFAULT_HYSTERESIS_SCORE = 15.0

Action = Literal["switch", "noop"]
Reason = Literal[
    "already-on-winner",
    "current-unhealthy",
    "score-improvement",
    "below-hysteresis",
    "no-current",
]

# Callable signature that returns ping_ms for a given IP. Decoupling the
# selector from `probe.py` keeps it pure and unit-testable: the CLI wires a
# real prober, tests pass a dict.
PingLookup = Callable[[str], float]
PenaltyLookup = Callable[[str], int]


class NoCandidatesError(RuntimeError):
    """Raised when filtering removes every server."""


@dataclass(frozen=True, slots=True)
class SelectorOptions:
    """Tunable knobs for candidate filtering and switching."""

    allowed_continents: tuple[str, ...] = DEFAULT_ALLOWED_CONTINENTS
    allowed_countries: tuple[str, ...] = ()
    max_load: int = DEFAULT_MAX_LOAD
    hysteresis_score: float = DEFAULT_HYSTERESIS_SCORE
    weights: ScoreWeights = field(default_factory=ScoreWeights)

    def __post_init__(self) -> None:
        """Normalize allowed_countries to lowercase so callers don't have to."""
        normalized = tuple(c.lower() for c in self.allowed_countries)
        object.__setattr__(self, "allowed_countries", normalized)


@dataclass(frozen=True, slots=True)
class Decision:
    """The picker's verdict for one run."""

    action: Action
    reason: Reason
    winner: Server
    endpoint_ip: str
    current_endpoint_ip: str | None
    current_server: Server | None
    candidates_count: int
    winner_score: float | None = None
    winner_ping_ms: float | None = None
    current_score: float | None = None


def filter_candidates(servers: list[Server], options: SelectorOptions) -> list[Server]:
    """Apply geo + health + load filters and return the survivors.

    Country allowlist takes precedence over continent: if any country codes are
    set, only those countries are considered, regardless of continent.
    """
    use_country_filter = bool(options.allowed_countries)

    def matches_geo(s: Server) -> bool:
        if use_country_filter:
            return s.country_code.lower() in options.allowed_countries
        return s.continent in options.allowed_continents

    return [
        s for s in servers if s.is_healthy and s.currentload <= options.max_load and matches_geo(s)
    ]


def _score_server(
    server: Server,
    ping_lookup: PingLookup,
    penalty_lookup: PenaltyLookup,
    weights: ScoreWeights,
) -> float:
    return score(
        ping_ms=ping_lookup(server.ip_v4_in1),
        load_pct=float(server.currentload),
        users_pct=server.users_pct,
        scorebase=float(server.scorebase),
        penalty=penalty_lookup(server.ip_v4_in1),
        weights=weights,
    )


def decide(
    servers: list[Server],
    current_endpoint_ip: str | None,
    options: SelectorOptions,
    *,
    ping_lookup: PingLookup,
    penalty_lookup: PenaltyLookup = lambda _ip: 0,
) -> Decision:
    """Decide whether to switch the WireGuard endpoint and to what.

    Args:
        servers: All servers returned by the AirVPN status API.
        current_endpoint_ip: IPv4 currently set on the WireGuard peer, or None
            if no endpoint is configured yet.
        options: Filtering and hysteresis options.
        ping_lookup: Callable mapping IP -> ping_ms (-1 if unknown).
        penalty_lookup: Callable mapping IP -> penalty count (default: 0).

    Returns:
        A Decision describing the outcome. action="switch" means the caller
        should run `wg set ... endpoint <decision.endpoint_ip>:<port>`.

    Raises:
        NoCandidatesError: if no server passes the filter.
    """
    candidates = filter_candidates(servers, options)
    if not candidates:
        raise NoCandidatesError(
            f"no servers matched filters "
            f"(continents={options.allowed_continents}, "
            f"countries={options.allowed_countries}, max_load={options.max_load})"
        )

    scored = [
        (s, _score_server(s, ping_lookup, penalty_lookup, options.weights)) for s in candidates
    ]
    scored.sort(key=lambda pair: pair[1])
    winner, winner_score = scored[0]
    candidates_count = len(scored)
    candidate_set = frozenset(id(s) for s, _ in scored)
    current_server = (
        _find_server_by_ip(servers, current_endpoint_ip) if current_endpoint_ip else None
    )
    current_score = (
        _score_server(current_server, ping_lookup, penalty_lookup, options.weights)
        if current_server is not None
        else None
    )
    winner_ping_raw = ping_lookup(winner.ip_v4_in1)
    winner_ping = winner_ping_raw if winner_ping_raw >= 0 else None

    # 1. No current endpoint at all -> switch unconditionally.
    if current_endpoint_ip is None:
        return Decision(
            action="switch",
            reason="no-current",
            winner=winner,
            endpoint_ip=winner.ip_v4_in1,
            current_endpoint_ip=None,
            current_server=None,
            candidates_count=candidates_count,
            winner_score=winner_score,
            winner_ping_ms=winner_ping,
            current_score=None,
        )

    # 2. Already on the winner (any of its IPs) -> no-op.
    if current_endpoint_ip in winner.ips_v4:
        return Decision(
            action="noop",
            reason="already-on-winner",
            winner=winner,
            endpoint_ip=current_endpoint_ip,
            current_endpoint_ip=current_endpoint_ip,
            current_server=current_server,
            candidates_count=candidates_count,
            winner_score=winner_score,
            winner_ping_ms=winner_ping,
            current_score=current_score,
        )

    # 3. Current endpoint is not in the candidate set -> switch.
    #
    # "Not in candidate set" covers all the cases we want to force-switch on:
    # current server is unknown to the API, unhealthy, overloaded, OR in a
    # country/continent the operator has explicitly excluded. All of those
    # mean "the operator wants off this server" and hysteresis must NOT
    # apply — see regression tests in test_selector.py.
    if current_server is None or id(current_server) not in candidate_set:
        return Decision(
            action="switch",
            reason="current-unhealthy",
            winner=winner,
            endpoint_ip=winner.ip_v4_in1,
            current_endpoint_ip=current_endpoint_ip,
            current_server=current_server,
            candidates_count=candidates_count,
            winner_score=winner_score,
            winner_ping_ms=winner_ping,
            current_score=current_score,
        )

    # 4. Hysteresis: only switch if the score improvement is meaningful.
    delta = (current_score or 0.0) - winner_score
    if delta < options.hysteresis_score:
        return Decision(
            action="noop",
            reason="below-hysteresis",
            winner=winner,
            endpoint_ip=current_endpoint_ip,
            current_endpoint_ip=current_endpoint_ip,
            current_server=current_server,
            candidates_count=candidates_count,
            winner_score=winner_score,
            winner_ping_ms=winner_ping,
            current_score=current_score,
        )

    return Decision(
        action="switch",
        reason="score-improvement",
        winner=winner,
        endpoint_ip=winner.ip_v4_in1,
        current_endpoint_ip=current_endpoint_ip,
        current_server=current_server,
        candidates_count=candidates_count,
        winner_score=winner_score,
        winner_ping_ms=winner_ping,
        current_score=current_score,
    )


def _find_server_by_ip(servers: list[Server], ip: str) -> Server | None:
    return next((s for s in servers if ip in s.ips_v4), None)
