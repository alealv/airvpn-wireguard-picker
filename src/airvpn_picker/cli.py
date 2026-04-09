"""Command-line entry point.

Glues together the API client, selector, wg wrapper, and state file. Designed
to be invoked from cron with no positional arguments — every knob is a flag
with a sensible default.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from airvpn_picker import __version__
from airvpn_picker.api import (
    DEFAULT_STATUS_URL,
    DEFAULT_TIMEOUT_SECONDS,
    AirVpnApiError,
    fetch_status,
)
from airvpn_picker.selector import (
    DEFAULT_HYSTERESIS_PP,
    DEFAULT_MAX_LOAD,
    Decision,
    NoCandidatesError,
    SelectorOptions,
    decide,
)
from airvpn_picker.state import append_log, save_state
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
            "Pick the fastest AirVPN WireGuard server by load and update a live wg peer endpoint."
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

    flap = parser.add_argument_group("anti-flap")
    flap.add_argument(
        "--hysteresis-pp",
        type=int,
        default=DEFAULT_HYSTERESIS_PP,
        help=(
            f"Min load delta in percentage points to trigger a switch "
            f"(default: {DEFAULT_HYSTERESIS_PP})"
        ),
    )
    flap.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help=f"Where to persist last decision (default: {DEFAULT_STATE_FILE})",
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


def _build_options(args: argparse.Namespace) -> SelectorOptions:
    return SelectorOptions(
        allowed_continents=_parse_csv(args.allowed_continents),
        allowed_countries=_parse_csv(args.allowed_countries),
        max_load=args.max_load,
        hysteresis_pp=args.hysteresis_pp,
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

    options = _build_options(args)

    try:
        current_ip = show_current_endpoint_ip(
            interface=args.interface,
            peer_pubkey=args.peer_pubkey,
        )
    except WgCommandError as exc:
        logger.error("could not read current wg endpoint: %s", exc)
        return EXIT_WG_FAILURE

    try:
        decision = decide(
            servers=servers,
            current_endpoint_ip=current_ip,
            options=options,
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
        save_state(args.state_file, decision)
        append_log(args.log_file, decision)

    return EXIT_OK


def _log_decision(decision: Decision) -> None:
    current = (
        f"{decision.current_server.public_name}@{decision.current_endpoint_ip} "
        f"(load={decision.current_server.currentload}%)"
        if decision.current_server
        else f"{decision.current_endpoint_ip or 'none'}"
    )
    logger.info(
        "%s [%s]: winner=%s@%s load=%d%%, current=%s, candidates=%d",
        decision.action,
        decision.reason,
        decision.winner.public_name,
        decision.endpoint_ip,
        decision.winner.currentload,
        current,
        decision.candidates_count,
    )
