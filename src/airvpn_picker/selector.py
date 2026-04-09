"""Filter candidate servers and decide whether to switch the WireGuard endpoint.

The decision logic is deliberately simple and pure (no I/O, no subprocess) so
that it can be exhaustively unit tested with synthetic and fixture-based inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from airvpn_picker.api import Server

DEFAULT_ALLOWED_CONTINENTS: tuple[str, ...] = ("Europe",)
DEFAULT_MAX_LOAD = 80
DEFAULT_HYSTERESIS_PP = 15

Action = Literal["switch", "noop"]
Reason = Literal[
    "already-on-winner",
    "current-unhealthy",
    "load-improvement",
    "below-hysteresis",
    "no-current",
]


class NoCandidatesError(RuntimeError):
    """Raised when filtering removes every server."""


@dataclass(frozen=True, slots=True)
class SelectorOptions:
    """Tunable knobs for candidate filtering and switching."""

    allowed_continents: tuple[str, ...] = DEFAULT_ALLOWED_CONTINENTS
    allowed_countries: tuple[str, ...] = ()
    max_load: int = DEFAULT_MAX_LOAD
    hysteresis_pp: int = DEFAULT_HYSTERESIS_PP

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


def _sort_key(server: Server) -> tuple[int, int, int]:
    return (server.currentload, server.users, server.bw)


def decide(
    servers: list[Server],
    current_endpoint_ip: str | None,
    options: SelectorOptions,
) -> Decision:
    """Decide whether to switch the WireGuard endpoint and to what.

    Args:
        servers: All servers returned by the AirVPN status API.
        current_endpoint_ip: IPv4 currently set on the WireGuard peer, or None
            if no endpoint is configured yet.
        options: Filtering and hysteresis options.

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

    candidates_sorted = sorted(candidates, key=_sort_key)
    winner = candidates_sorted[0]
    candidates_count = len(candidates_sorted)
    current_server = (
        _find_server_by_ip(servers, current_endpoint_ip) if current_endpoint_ip else None
    )

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
        )

    # 3. Current endpoint is not in the candidate set (unhealthy or unknown) -> switch.
    if current_server is None or not _is_acceptable(current_server, options):
        return Decision(
            action="switch",
            reason="current-unhealthy",
            winner=winner,
            endpoint_ip=winner.ip_v4_in1,
            current_endpoint_ip=current_endpoint_ip,
            current_server=current_server,
            candidates_count=candidates_count,
        )

    # 4. Hysteresis: only switch if the load improvement is meaningful.
    delta = current_server.currentload - winner.currentload
    if delta < options.hysteresis_pp:
        return Decision(
            action="noop",
            reason="below-hysteresis",
            winner=winner,
            endpoint_ip=current_endpoint_ip,
            current_endpoint_ip=current_endpoint_ip,
            current_server=current_server,
            candidates_count=candidates_count,
        )

    return Decision(
        action="switch",
        reason="load-improvement",
        winner=winner,
        endpoint_ip=winner.ip_v4_in1,
        current_endpoint_ip=current_endpoint_ip,
        current_server=current_server,
        candidates_count=candidates_count,
    )


def _is_acceptable(server: Server, options: SelectorOptions) -> bool:
    return server.is_healthy and server.currentload <= options.max_load


def _find_server_by_ip(servers: list[Server], ip: str) -> Server | None:
    return next((s for s in servers if ip in s.ips_v4), None)
