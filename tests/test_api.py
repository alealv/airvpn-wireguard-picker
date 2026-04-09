"""Tests for the AirVPN API client and parser."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest

from airvpn_picker.api import (
    DEFAULT_STATUS_URL,
    AirVpnApiError,
    Server,
    fetch_status,
    parse_status,
)


class TestParseStatus:
    def test_parses_real_fixture(self, status_sample: dict[str, Any]) -> None:
        servers = parse_status(status_sample)
        assert len(servers) == 255
        assert all(isinstance(s, Server) for s in servers)

    def test_server_fields_populated(self, status_sample: dict[str, Any]) -> None:
        servers = parse_status(status_sample)
        first = servers[0]
        assert first.public_name
        assert first.country_code
        assert first.continent
        assert first.health in {"ok", "warning", "error"}
        assert 0 <= first.currentload <= 200  # API can report >100
        assert first.ip_v4_in1
        assert first.ips_v4  # at least one IPv4

    def test_collects_all_v4_ips(self) -> None:
        payload = {
            "result": "ok",
            "servers": [
                {
                    "public_name": "Test",
                    "country_name": "Germany",
                    "country_code": "de",
                    "continent": "Europe",
                    "location": "Frankfurt",
                    "bw": 100,
                    "bw_max": 1000,
                    "users": 50,
                    "currentload": 10,
                    "ip_v4_in1": "1.1.1.1",
                    "ip_v4_in2": "1.1.1.2",
                    "ip_v4_in3": "1.1.1.3",
                    "ip_v4_in4": "1.1.1.4",
                    "health": "ok",
                },
            ],
        }
        servers = parse_status(payload)
        assert servers[0].ips_v4 == ("1.1.1.1", "1.1.1.2", "1.1.1.3", "1.1.1.4")

    def test_handles_missing_optional_v4_ips(self) -> None:
        payload = {
            "result": "ok",
            "servers": [
                {
                    "public_name": "Sparse",
                    "country_name": "Germany",
                    "country_code": "de",
                    "continent": "Europe",
                    "location": "Frankfurt",
                    "bw": 100,
                    "bw_max": 1000,
                    "users": 50,
                    "currentload": 10,
                    "ip_v4_in1": "1.1.1.1",
                    "health": "ok",
                },
            ],
        }
        servers = parse_status(payload)
        assert servers[0].ips_v4 == ("1.1.1.1",)

    def test_skips_servers_with_invalid_ipv4(self) -> None:
        payload = {
            "result": "ok",
            "servers": [
                {
                    "public_name": "Bad",
                    "country_name": "Germany",
                    "country_code": "de",
                    "continent": "Europe",
                    "location": "Frankfurt",
                    "bw": 100,
                    "bw_max": 1000,
                    "users": 50,
                    "currentload": 10,
                    "ip_v4_in1": "not-an-ip",
                    "health": "ok",
                },
            ],
        }
        servers = parse_status(payload)
        assert servers == []

    def test_raises_on_non_ok_result(self) -> None:
        with pytest.raises(AirVpnApiError, match="result"):
            parse_status({"result": "error", "servers": []})

    def test_raises_on_missing_servers_key(self) -> None:
        with pytest.raises(AirVpnApiError, match="servers"):
            parse_status({"result": "ok"})

    def test_skips_server_missing_required_field(self) -> None:
        # If a server is missing public_name we drop it rather than crash.
        payload = {
            "result": "ok",
            "servers": [
                {
                    "country_name": "Germany",
                    "country_code": "de",
                    "continent": "Europe",
                    "location": "Frankfurt",
                    "bw": 100,
                    "bw_max": 1000,
                    "users": 50,
                    "currentload": 10,
                    "ip_v4_in1": "1.1.1.1",
                    "health": "ok",
                },
            ],
        }
        servers = parse_status(payload)
        assert servers == []


class TestFetchStatus:
    def test_fetches_and_parses(self, status_sample: dict[str, Any]) -> None:
        body = json.dumps(status_sample).encode()
        mock_response = MagicMock()
        mock_response.read.return_value = body
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = None

        with patch("airvpn_picker.api.urlopen", return_value=mock_response) as m:
            servers = fetch_status(timeout=5)

        assert len(servers) == 255
        m.assert_called_once()
        called_request = m.call_args.args[0]
        assert called_request.full_url == DEFAULT_STATUS_URL
        assert m.call_args.kwargs["timeout"] == 5

    def test_raises_on_network_error(self) -> None:
        with (
            patch("airvpn_picker.api.urlopen", side_effect=URLError("dns fail")),
            pytest.raises(AirVpnApiError, match="dns fail"),
        ):
            fetch_status()

    def test_raises_on_invalid_json(self) -> None:
        mock_response = MagicMock()
        mock_response.read.return_value = b"not json"
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = None

        with (
            patch("airvpn_picker.api.urlopen", return_value=mock_response),
            pytest.raises(AirVpnApiError, match="JSON"),
        ):
            fetch_status()
