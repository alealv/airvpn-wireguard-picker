"""Client for the public AirVPN status API.

The API is documented at https://airvpn.org/faq/api/ and requires no
authentication. We use the standard library only (urllib + json).
"""

from __future__ import annotations

import ipaddress
import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

DEFAULT_STATUS_URL = "https://airvpn.org/api/status/"
DEFAULT_USER_AGENT = (
    "airvpn-wireguard-picker/0.1 (+https://github.com/alealv/airvpn-wireguard-picker)"
)
DEFAULT_TIMEOUT_SECONDS = 10

_REQUIRED_FIELDS = (
    "public_name",
    "country_code",
    "continent",
    "currentload",
    "health",
    "ip_v4_in1",
)

logger = logging.getLogger(__name__)


class AirVpnApiError(RuntimeError):
    """Raised when fetching or parsing the AirVPN status API fails."""


@dataclass(frozen=True, slots=True)
class Server:
    """Subset of an AirVPN server entry that the picker cares about."""

    public_name: str
    country_code: str
    country_name: str
    continent: str
    location: str
    health: str
    currentload: int
    users: int
    users_max: int
    bw: int
    bw_max: int
    scorebase: int
    ips_v4: tuple[str, ...]

    @property
    def users_pct(self) -> float:
        """Percentage of cap (0..100). Returns currentload as fallback if cap is 0."""
        if self.users_max <= 0:
            return float(self.currentload)
        return min(100.0, 100.0 * self.users / self.users_max)

    @property
    def ip_v4_in1(self) -> str:
        """The primary IPv4 entry IP — what `wg set` will use."""
        return self.ips_v4[0]

    @property
    def is_healthy(self) -> bool:
        """True if AirVPN reports this server as healthy."""
        return self.health == "ok"


def fetch_status(
    url: str = DEFAULT_STATUS_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[Server]:
    """Fetch the AirVPN status payload and return parsed servers.

    Raises:
        AirVpnApiError: on network failure, non-JSON response, or non-ok result.
    """
    request = Request(url, headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"})  # noqa: S310
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read()
    except URLError as exc:
        raise AirVpnApiError(f"failed to fetch {url}: {exc.reason}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AirVpnApiError(f"invalid JSON from {url}: {exc}") from exc

    return parse_status(payload)


def parse_status(payload: dict[str, Any]) -> list[Server]:
    """Convert the raw API payload into a list of Server records.

    Drops any server that is missing a required field or whose primary IPv4
    entry IP cannot be parsed. Logs a debug line for each drop.
    """
    if payload.get("result") != "ok":
        raise AirVpnApiError(f"API result was not 'ok': {payload.get('result')!r}")

    raw_servers = payload.get("servers")
    if not isinstance(raw_servers, list):
        raise AirVpnApiError("API payload missing 'servers' list")

    servers: list[Server] = []
    for entry in raw_servers:
        server = _build_server(entry)
        if server is not None:
            servers.append(server)
    return servers


def _build_server(entry: dict[str, Any]) -> Server | None:
    if not all(field in entry for field in _REQUIRED_FIELDS):
        logger.debug("dropping server entry missing required fields: %s", entry.get("public_name"))
        return None

    ips_v4 = _collect_ipv4s(entry)
    if not ips_v4:
        logger.debug("dropping server %s: no valid IPv4 entry", entry.get("public_name"))
        return None

    return Server(
        public_name=str(entry["public_name"]),
        country_code=str(entry["country_code"]),
        country_name=str(entry.get("country_name", "")),
        continent=str(entry["continent"]),
        location=str(entry.get("location", "")),
        health=str(entry["health"]),
        currentload=int(entry["currentload"]),
        users=int(entry.get("users", 0)),
        users_max=int(entry.get("users_max", 0)),
        bw=int(entry.get("bw", 0)),
        bw_max=int(entry.get("bw_max", 0)),
        scorebase=int(entry.get("scorebase", 0)),
        ips_v4=ips_v4,
    )


def _collect_ipv4s(entry: dict[str, Any]) -> tuple[str, ...]:
    ips: list[str] = []
    for key in ("ip_v4_in1", "ip_v4_in2", "ip_v4_in3", "ip_v4_in4"):
        value = entry.get(key)
        if not value:
            continue
        try:
            ipaddress.IPv4Address(str(value))
        except ValueError:
            continue
        ips.append(str(value))
    return tuple(ips)
