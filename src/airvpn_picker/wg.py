"""Thin wrapper around the `wg` CLI for showing and setting peer endpoints.

Only the operations the picker needs are implemented:

- ``show_current_endpoint_ip`` parses ``wg show <iface> endpoints``
- ``set_endpoint`` runs ``wg set <iface> peer <key> endpoint <ip>:<port>``

Both functions use ``subprocess.run`` with explicit argument lists (no shell)
and a 10-second timeout. Failures are surfaced as ``WgCommandError``.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import logging
import re
import subprocess
from typing import Final

DEFAULT_WG_BINARY = "wg"
DEFAULT_TIMEOUT_SECONDS: Final = 10
PEER_PUBKEY_LEN: Final = 44  # base64-encoded 32-byte WireGuard key length
PEER_PUBKEY_DECODED_LEN: Final = 32  # raw bytes after base64 decode
_ENDPOINT_LINE_FIELDS: Final = 2  # "<pubkey>\t<endpoint>"

_NONE_LITERALS = frozenset({"", "(none)"})
_IPV6_BRACKETED = re.compile(r"^\[(?P<addr>[^\]]+)\]:(?P<port>\d+)$")
_IPV4_PORT = re.compile(r"^(?P<addr>[^:]+):(?P<port>\d+)$")

logger = logging.getLogger(__name__)


class WgCommandError(RuntimeError):
    """Raised when a wg(8) invocation fails."""


def validate_pubkey(key: str) -> None:
    """Raise ValueError if `key` is not a syntactically valid wg public key."""
    if len(key) != PEER_PUBKEY_LEN:
        raise ValueError(
            f"WireGuard public key must be {PEER_PUBKEY_LEN} characters base64; got {len(key)}"
        )
    try:
        decoded = base64.b64decode(key, validate=True)
    except binascii.Error as exc:
        raise ValueError(f"WireGuard public key is not valid base64: {exc}") from exc
    if len(decoded) != PEER_PUBKEY_DECODED_LEN:
        raise ValueError(f"WireGuard public key must decode to {PEER_PUBKEY_DECODED_LEN} bytes")


def parse_endpoint(value: str) -> tuple[str, int] | None:
    """Parse a wg endpoint string like '1.2.3.4:51820' or '[::1]:51820' or '(none)'."""
    stripped = value.strip()
    if stripped in _NONE_LITERALS:
        return None
    if (m := _IPV6_BRACKETED.match(stripped)) is not None:
        return m.group("addr"), int(m.group("port"))
    if (m := _IPV4_PORT.match(stripped)) is not None:
        return m.group("addr"), int(m.group("port"))
    raise ValueError(f"cannot parse wg endpoint: {value!r}")


def parse_endpoints_output(output: str) -> dict[str, tuple[str, int] | None]:
    """Parse the output of ``wg show <iface> endpoints`` into a dict by pubkey."""
    result: dict[str, tuple[str, int] | None] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Lines are <pubkey>\t<endpoint>; tabs separate fields per wg(8).
        parts = line.split("\t")
        if len(parts) != _ENDPOINT_LINE_FIELDS:
            logger.debug("ignoring unparseable wg show line: %r", line)
            continue
        pubkey, endpoint = parts
        result[pubkey] = parse_endpoint(endpoint)
    return result


def show_current_endpoint_ip(
    interface: str,
    peer_pubkey: str,
    wg_binary: str = DEFAULT_WG_BINARY,
) -> str | None:
    """Return the IP currently set as the endpoint for `peer_pubkey` on `interface`.

    Returns None if the peer exists but has no endpoint, or if the peer is not
    on the interface at all.
    """
    cmd = [wg_binary, "show", interface, "endpoints"]
    logger.debug("running: %s", cmd)
    try:
        completed = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as exc:
        raise WgCommandError(
            f"`{' '.join(cmd)}` failed (exit {exc.returncode}): {exc.stderr.strip()}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise WgCommandError(f"`{' '.join(cmd)}` timed out after {exc.timeout}s") from exc

    endpoints = parse_endpoints_output(completed.stdout)
    endpoint = endpoints.get(peer_pubkey)
    if endpoint is None:
        return None
    return endpoint[0]


def set_endpoint(
    interface: str,
    peer_pubkey: str,
    ip: str,
    port: int,
    wg_binary: str = DEFAULT_WG_BINARY,
    *,
    dry_run: bool = False,
) -> None:
    """Set the endpoint of `peer_pubkey` on `interface` to `ip`:`port`.

    Live, atomic operation: existing tunnel state is preserved and active TCP
    flows survive because the interface is not restarted.
    """
    endpoint = _format_endpoint(ip, port)
    cmd = [wg_binary, "set", interface, "peer", peer_pubkey, "endpoint", endpoint]

    if dry_run:
        logger.info("[dry-run] would run: %s", " ".join(cmd))
        return

    logger.debug("running: %s", cmd)
    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as exc:
        raise WgCommandError(
            f"`{' '.join(cmd)}` failed (exit {exc.returncode}): {exc.stderr.strip()}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise WgCommandError(f"`{' '.join(cmd)}` timed out after {exc.timeout}s") from exc


def _format_endpoint(ip: str, port: int) -> str:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError as exc:
        raise ValueError(f"invalid endpoint IP: {ip!r}") from exc
    if isinstance(addr, ipaddress.IPv6Address):
        return f"[{ip}]:{port}"
    return f"{ip}:{port}"
