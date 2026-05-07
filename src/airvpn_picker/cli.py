"""Command-line entry point.

Glues together the API client, ICMP probe, scoring, selector, wg wrapper,
and state file. Designed to be invoked from cron with no positional
arguments — every knob is a flag with a sensible default.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from airvpn_picker import __version__
from airvpn_picker.api import (
    DEFAULT_STATUS_URL,
    DEFAULT_TIMEOUT_SECONDS,
    AirVpnApiError,
    fetch_status,
)
from airvpn_picker.probe import (
    DEFAULT_PING_COUNT,
    DEFAULT_PING_TIMEOUT_S,
    ping_many,
)
from airvpn_picker.scoring import ScoreWeights
from airvpn_picker.selector import (
    DEFAULT_HYSTERESIS_SCORE,
    DEFAULT_MAX_LOAD,
    Decision,
    NoCandidatesError,
    SelectorOptions,
    decide,
    filter_candidates,
)
from airvpn_picker.state import (
    PING_CACHE_TTL_S,
    append_log,
    cached_ping,
    decay_penalties,
    load_state,
    merge_ping_cache,
    penalty_for,
    save_state,
    stale_ips,
)
from airvpn_picker.wg import WgCommandError, set_endpoint, show_current_endpoint_ip, validate_pubkey

DEFAULT_PORT = 1637
DEFAULT_LOG_FILE = Path("/var/log/airvpn-picker.log")
DEFAULT_STATE_FILE = Path("/var/db/airvpn-picker.json")
DEFAULT_CONTINENTS = "Europe"

EXIT_OK = 0
EXIT_API_FAILURE = 1
EXIT_NO_CANDIDATES = 2
EXIT_WG_FAILURE = 3
EXIT_BAD_ARGS = 4

logger = logging.getLogger("airvpn_picker")


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser used by `main`."""
    parser = argparse.ArgumentParser(
        prog="airvpn-picker",
        description=(
            "Pick the fastest AirVPN WireGuard server by Eddie-style score "
            "(ping + load + users + scorebase + penalty) and update a live "
            "wg peer endpoint."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    required = parser.add_argument_group("required")
    required.add_argument("--interface", required=True, help="WireGuard interface, e.g. wg2")
    required.add_argument(
        "--peer-pubkey",
        required=True,
        help="Public key of the AirVPN peer to update",
    )

    selection = parser.add_argument_group("selection")
    selection.add_argument(
        "--allowed-continents",
        default=DEFAULT_CONTINENTS,
        help=f"Comma-separated continents to allow (default: {DEFAULT_CONTINENTS})",
    )
    selection.add_argument(
        "--allowed-countries",
        default="",
        help="Comma-separated country codes; overrides --allowed-continents",
    )
    selection.add_argument(
        "--max-load",
        type=int,
        default=DEFAULT_MAX_LOAD,
        help=f"Skip servers with currentload > N percent (default: {DEFAULT_MAX_LOAD})",
    )
    selection.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Endpoint port to set on the peer (default: {DEFAULT_PORT})",
    )

    probe = parser.add_argument_group("ping probe")
    probe.add_argument(
        "--probe-ping",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Measure ICMP RTT to each candidate (default: enabled)",
    )
    probe.add_argument(
        "--ping-count",
        type=int,
        default=DEFAULT_PING_COUNT,
        help=f"ICMP echos per candidate (default: {DEFAULT_PING_COUNT})",
    )
    probe.add_argument(
        "--ping-timeout",
        type=float,
        default=DEFAULT_PING_TIMEOUT_S,
        help=f"Per-candidate ping timeout in seconds (default: {DEFAULT_PING_TIMEOUT_S})",
    )
    probe.add_argument(
        "--ping-cache-ttl",
        type=float,
        default=PING_CACHE_TTL_S,
        help=(
            f"Re-probe an IP only if its cached reading is older than N seconds "
            f"(default: {int(PING_CACHE_TTL_S)})"
        ),
    )

    scoring = parser.add_argument_group("scoring (Eddie-compatible)")
    scoring.add_argument(
        "--score-mode",
        choices=("speed", "latency"),
        default="speed",
        help="speed: ping/load/users/scorebase weighted equally; latency: ping dominates",
    )
    scoring.add_argument("--ping-factor", type=float, default=1.0, help="Weight on ping_ms")
    scoring.add_argument("--load-factor", type=float, default=1.0, help="Weight on load_pct")
    scoring.add_argument("--users-factor", type=float, default=1.0, help="Weight on users_pct")
    scoring.add_argument(
        "--penalty-factor",
        type=float,
        default=1000.0,
        help="Per-failure penalty added to score (default: 1000)",
    )

    flap = parser.add_argument_group("anti-flap")
    flap.add_argument(
        "--hysteresis-score",
        type=float,
        default=DEFAULT_HYSTERESIS_SCORE,
        help=(
            f"Min score delta to trigger a switch (default: {DEFAULT_HYSTERESIS_SCORE})."
            " Switch only if winner_score < current_score - delta."
        ),
    )
    flap.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help=f"Where to persist last decision + caches (default: {DEFAULT_STATE_FILE})",
    )

    op = parser.add_argument_group("operational")
    op.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the decision but do not run wg set",
    )
    op.add_argument(
        "--log-file",
        type=Path,
        default=DEFAULT_LOG_FILE,
        help=f"Append JSON log line per run (default: {DEFAULT_LOG_FILE})",
    )
    op.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity to stderr (default: INFO)",
    )
    op.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout for the AirVPN API call (default: {DEFAULT_TIMEOUT_SECONDS}s)",
    )
    op.add_argument(
        "--status-url",
        default=DEFAULT_STATUS_URL,
        help=argparse.SUPPRESS,  # for tests / advanced use only
    )
    return parser


def _parse_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _build_weights(args: argparse.Namespace) -> ScoreWeights:
    return ScoreWeights(
        mode=args.score_mode,
        ping_factor=args.ping_factor,
        load_factor=args.load_factor,
        users_factor=args.users_factor,
        penalty_factor=args.penalty_factor,
    )


def _build_options(args: argparse.Namespace, weights: ScoreWeights) -> SelectorOptions:
    return SelectorOptions(
        allowed_continents=_parse_csv(args.allowed_continents),
        allowed_countries=_parse_csv(args.allowed_countries),
        max_load=args.max_load,
        hysteresis_score=args.hysteresis_score,
        weights=weights,
    )


def main(argv: list[str] | None = None) -> int:
    """Run the picker. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.log_level)

    try:
        validate_pubkey(args.peer_pubkey)
    except ValueError as exc:
        logger.error("invalid --peer-pubkey: %s", exc)
        return EXIT_BAD_ARGS

    try:
        servers = fetch_status(url=args.status_url, timeout=args.timeout)
    except AirVpnApiError as exc:
        logger.error("AirVPN API error: %s", exc)
        return EXIT_API_FAILURE

    weights = _build_weights(args)
    options = _build_options(args, weights)
    now = time.time()

    state = load_state(args.state_file)
    ping_cache = state.ping_cache if state else {}
    penalties = decay_penalties(state.penalties if state else {}, now)

    try:
        current_ip = show_current_endpoint_ip(
            interface=args.interface,
            peer_pubkey=args.peer_pubkey,
        )
    except WgCommandError as exc:
        logger.error("could not read current wg endpoint: %s", exc)
        return EXIT_WG_FAILURE

    # Probe pings only for candidates the selector would actually consider.
    # No point measuring 200 unhealthy / out-of-region servers we'll discard.
    if args.probe_ping:
        eligible = filter_candidates(servers, options)
        eligible_ips = {s.ip_v4_in1 for s in eligible}
        # Always probe the current endpoint even if filtered out, so we can
        # score it against winners (Eddie does this so a degraded current is
        # detected promptly).
        if current_ip:
            eligible_ips.add(current_ip)
        cache_misses = stale_ips(ping_cache, now, args.ping_cache_ttl)
        to_probe = sorted(ip for ip in eligible_ips if ip in cache_misses or ip not in ping_cache)
        if to_probe:
            logger.debug("probing %d IPs", len(to_probe))
            fresh = ping_many(to_probe, args.ping_count, args.ping_timeout)
            ping_cache = merge_ping_cache(ping_cache, fresh, now)

    def _ping_lookup(ip: str) -> float:
        return cached_ping(ping_cache, ip)

    def _penalty_lookup(ip: str) -> int:
        return penalty_for(penalties, ip)

    try:
        decision = decide(
            servers=servers,
            current_endpoint_ip=current_ip,
            options=options,
            ping_lookup=_ping_lookup,
            penalty_lookup=_penalty_lookup,
        )
    except NoCandidatesError as exc:
        logger.error("%s", exc)
        return EXIT_NO_CANDIDATES

    _log_decision(decision)

    if decision.action == "switch":
        try:
            set_endpoint(
                interface=args.interface,
                peer_pubkey=args.peer_pubkey,
                ip=decision.endpoint_ip,
                port=args.port,
                dry_run=args.dry_run,
            )
        except WgCommandError as exc:
            logger.error("wg set failed: %s", exc)
            return EXIT_WG_FAILURE

    if not args.dry_run:
        save_state(
            args.state_file,
            decision,
            ping_cache=ping_cache,
            penalties=penalties,
        )
        append_log(args.log_file, decision)

    return EXIT_OK


def _log_decision(decision: Decision) -> None:
    current = (
        f"{decision.current_server.public_name}@{decision.current_endpoint_ip} "
        f"score={decision.current_score:.1f}"
        if decision.current_server and decision.current_score is not None
        else f"{decision.current_endpoint_ip or 'none'}"
    )
    ping_str = (
        f"ping={decision.winner_ping_ms:.1f}ms" if decision.winner_ping_ms is not None else "ping=?"
    )
    score_str = (
        f"score={decision.winner_score:.1f}" if decision.winner_score is not None else "score=?"
    )
    logger.info(
        "%s [%s]: winner=%s@%s load=%d%% %s %s, current=%s, candidates=%d",
        decision.action,
        decision.reason,
        decision.winner.public_name,
        decision.endpoint_ip,
        decision.winner.currentload,
        ping_str,
        score_str,
        current,
        decision.candidates_count,
    )
