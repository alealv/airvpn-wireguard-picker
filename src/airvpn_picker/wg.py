"""Thin wrapper around the `wg` CLI for showing and setting peer endpoints.

Only the operations the picker needs are implemented:

- ``show_current_endpoint_ip`` parses ``wg show <iface> endpoints``
- ``set_endpoint`` removes the peer and re-adds it with the new endpoint,
  using the PSK already loaded in the kernel (read via ``wg show preshared-keys``).
  The remove+readd sequence tears down the existing WireGuard session so the
  new endpoint cannot be overwritten by WireGuard's automatic roaming.

All functions use ``subprocess.run`` with explicit argument lists (no shell)
and a 10-second timeout. Failures are surfaced as ``WgCommandError``.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Final

DEFAULT_WG_BINARY = "wg"
DEFAULT_TIMEOUT_SECONDS: Final = 10
DEFAULT_ALLOWED_IPS: Final = "0.0.0.0/0,::/0"
DEFAULT_PERSISTENT_KEEPALIVE: Final = 25
PEER_PUBKEY_LEN: Final = 44  # base64-encoded 32-byte WireGuard key length
PEER_PUBKEY_DECODED_LEN: Final = 32  # raw bytes after base64 decode
_ENDPOINT_LINE_FIELDS: Final = 2  # "<pubkey>\t<value>"

_NONE_LITERALS = frozenset({"", "(none)", "off"})
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


def _parse_tab_output(output: str) -> dict[str, str]:
    r"""Parse ``wg show <iface> <field>`` output into a dict by pubkey.

    Lines are ``<pubkey>\t<value>``. Values that represent "none" map to "".
    """
    result: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != _ENDPOINT_LINE_FIELDS:
            logger.debug("ignoring unparseable wg show line: %r", line)
            continue
        pubkey, value = parts
        result[pubkey] = "" if value.strip() in _NONE_LITERALS else value.strip()
    return result


def _run_wg(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a `wg` command, raising :class:`WgCommandError` on any failure."""
    logger.debug("running: %s", cmd)
    try:
        return subprocess.run(
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


def show_current_endpoint_ip(
    interface: str,
    peer_pubkey: str,
    wg_binary: str = DEFAULT_WG_BINARY,
) -> str | None:
    """Return the IP currently set as the endpoint for `peer_pubkey` on `interface`.

    Returns None if the peer exists but has no endpoint, or if the peer is not
    on the interface at all.
    """
    completed = _run_wg([wg_binary, "show", interface, "endpoints"])
    endpoint = parse_endpoints_output(completed.stdout).get(peer_pubkey)
    return endpoint[0] if endpoint is not None else None


def _read_peer_psk(
    interface: str,
    peer_pubkey: str,
    wg_binary: str,
) -> str:
    """Read the preshared key for `peer_pubkey` from the kernel via ``wg show``.

    Returns the PSK as a base64 string, or "" if none is configured.
    """
    completed = _run_wg([wg_binary, "show", interface, "preshared-keys"])
    return _parse_tab_output(completed.stdout).get(peer_pubkey, "")


def _read_peer_allowed_ips(
    interface: str,
    peer_pubkey: str,
    wg_binary: str,
) -> str:
    """Read the allowed-ips for `peer_pubkey` from the kernel via ``wg show``.

    Returns a comma-separated CIDR string, or the default ``0.0.0.0/0,::/0``.
    """
    completed = _run_wg([wg_binary, "show", interface, "allowed-ips"])
    raw = _parse_tab_output(completed.stdout).get(peer_pubkey, "")
    if not raw:
        return DEFAULT_ALLOWED_IPS
    # wg show outputs allowed-ips as space-separated on one line per peer.
    return ",".join(raw.split())


def _read_peer_keepalive(
    interface: str,
    peer_pubkey: str,
    wg_binary: str,
) -> int:
    """Read the persistent-keepalive for `peer_pubkey` from the kernel.

    Returns the integer value, or ``DEFAULT_PERSISTENT_KEEPALIVE`` if not set.
    """
    completed = _run_wg([wg_binary, "show", interface, "persistent-keepalive"])
    raw = _parse_tab_output(completed.stdout).get(peer_pubkey, "")
    if not raw or raw == "off":
        return DEFAULT_PERSISTENT_KEEPALIVE
    try:
        return int(raw)
    except ValueError:
        logger.debug("could not parse keepalive %r, using default", raw)
        return DEFAULT_PERSISTENT_KEEPALIVE


def set_endpoint(
    interface: str,
    peer_pubkey: str,
    ip: str,
    port: int,
    wg_binary: str = DEFAULT_WG_BINARY,
    *,
    dry_run: bool = False,
) -> None:
    """Switch the peer endpoint using remove + re-add to prevent roaming reversion.

    WireGuard's automatic endpoint roaming (per wg(8)) will overwrite a naked
    ``wg set ... endpoint`` within seconds if the old session is still active.
    Removing the peer first tears down the session keys so the new endpoint
    holds permanently.

    The PSK, allowed-ips, and persistent-keepalive are read from the kernel
    (``wg show <iface> preshared-keys/allowed-ips/persistent-keepalive``) so
    no extra CLI flags or config files are needed.
    """
    endpoint_str = _format_endpoint(ip, port)

    psk = _read_peer_psk(interface, peer_pubkey, wg_binary)
    allowed_ips = _read_peer_allowed_ips(interface, peer_pubkey, wg_binary)
    keepalive = _read_peer_keepalive(interface, peer_pubkey, wg_binary)

    remove_cmd = [wg_binary, "set", interface, "peer", peer_pubkey, "remove"]
    readd_cmd = [
        wg_binary,
        "set",
        interface,
        "peer",
        peer_pubkey,
        "allowed-ips",
        allowed_ips,
        "persistent-keepalive",
        str(keepalive),
        "endpoint",
        endpoint_str,
    ]

    if dry_run:
        logger.info("[dry-run] would run: %s", " ".join(remove_cmd))
        if psk:
            logger.info("[dry-run] would run: %s [preshared-key <tmpfile>]", " ".join(readd_cmd))
        else:
            logger.info("[dry-run] would run: %s", " ".join(readd_cmd))
        return

    _run_wg(remove_cmd)

    if psk:
        # Write PSK to a temp file; wg requires a file path, not inline value.
        # The file is deleted immediately after the wg set call.
        fd, psk_path = tempfile.mkstemp(prefix="airvpn-picker-psk-")
        try:
            os.write(fd, psk.encode())
            os.close(fd)
            readd_cmd_with_psk = [*readd_cmd, "preshared-key", psk_path]
            _run_wg(readd_cmd_with_psk)
        finally:
            Path(psk_path).unlink()
    else:
        _run_wg(readd_cmd)


def _format_endpoint(ip: str, port: int) -> str:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError as exc:
        raise ValueError(f"invalid endpoint IP: {ip!r}") from exc
    if isinstance(addr, ipaddress.IPv6Address):
        return f"[{ip}]:{port}"
    return f"{ip}:{port}"
