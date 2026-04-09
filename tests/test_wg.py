"""Tests for the WireGuard subprocess wrapper."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from airvpn_picker.wg import (
    DEFAULT_WG_BINARY,
    PEER_PUBKEY_LEN,
    WgCommandError,
    parse_endpoint,
    parse_endpoints_output,
    set_endpoint,
    show_current_endpoint_ip,
    validate_pubkey,
)

PEER_KEY = "PyLCXAQT8KkM4T+dUsOQfn+Ub3pGxfGlxkIApuig+hk="


class TestParseEndpoint:
    def test_ipv4(self) -> None:
        assert parse_endpoint("213.152.161.213:1637") == ("213.152.161.213", 1637)

    def test_ipv6_bracketed(self) -> None:
        assert parse_endpoint("[2001:db8::1]:1637") == ("2001:db8::1", 1637)

    def test_none_literal(self) -> None:
        assert parse_endpoint("(none)") is None
        assert parse_endpoint("") is None

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="cannot parse"):
            parse_endpoint("garbage")


class TestParseEndpointsOutput:
    def test_single_peer(self) -> None:
        output = f"{PEER_KEY}\t213.152.161.213:1637\n"
        result = parse_endpoints_output(output)
        assert result == {PEER_KEY: ("213.152.161.213", 1637)}

    def test_skips_malformed_lines(self) -> None:
        # A line with no tab separator is logged and skipped, not raised.
        output = f"garbage-line\n{PEER_KEY}\t1.2.3.4:1637\n"
        result = parse_endpoints_output(output)
        assert result == {PEER_KEY: ("1.2.3.4", 1637)}

    def test_multiple_peers(self) -> None:
        other_key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
        output = f"{PEER_KEY}\t213.152.161.213:1637\n{other_key}\t1.2.3.4:51820\n"
        result = parse_endpoints_output(output)
        assert result[PEER_KEY] == ("213.152.161.213", 1637)
        assert result[other_key] == ("1.2.3.4", 51820)

    def test_peer_with_no_endpoint(self) -> None:
        output = f"{PEER_KEY}\t(none)\n"
        result = parse_endpoints_output(output)
        assert result == {PEER_KEY: None}

    def test_empty_output(self) -> None:
        assert parse_endpoints_output("") == {}
        assert parse_endpoints_output("\n") == {}


class TestShowCurrentEndpointIp:
    def _mock_run(self, stdout: str, returncode: int = 0):
        return subprocess.CompletedProcess(
            args=["wg", "show", "wg2", "endpoints"],
            returncode=returncode,
            stdout=stdout,
            stderr="",
        )

    def test_returns_ip_for_known_peer(self) -> None:
        with patch(
            "airvpn_picker.wg.subprocess.run",
            return_value=self._mock_run(f"{PEER_KEY}\t213.152.161.213:1637\n"),
        ) as run:
            ip = show_current_endpoint_ip(interface="wg2", peer_pubkey=PEER_KEY)
        assert ip == "213.152.161.213"
        run.assert_called_once_with(
            [DEFAULT_WG_BINARY, "show", "wg2", "endpoints"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_returns_none_when_endpoint_unset(self) -> None:
        with patch(
            "airvpn_picker.wg.subprocess.run",
            return_value=self._mock_run(f"{PEER_KEY}\t(none)\n"),
        ):
            assert show_current_endpoint_ip(interface="wg2", peer_pubkey=PEER_KEY) is None

    def test_returns_none_when_peer_absent(self) -> None:
        with patch(
            "airvpn_picker.wg.subprocess.run",
            return_value=self._mock_run("OTHERKEY=\t1.2.3.4:51820\n"),
        ):
            assert show_current_endpoint_ip(interface="wg2", peer_pubkey=PEER_KEY) is None

    def test_raises_when_wg_command_fails(self) -> None:
        with (
            patch(
                "airvpn_picker.wg.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "wg", stderr="boom"),
            ),
            pytest.raises(WgCommandError, match="boom"),
        ):
            show_current_endpoint_ip(interface="wg2", peer_pubkey=PEER_KEY)

    def test_raises_when_wg_command_times_out(self) -> None:
        with (
            patch(
                "airvpn_picker.wg.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="wg", timeout=10),
            ),
            pytest.raises(WgCommandError, match="timed out"),
        ):
            show_current_endpoint_ip(interface="wg2", peer_pubkey=PEER_KEY)


class TestSetEndpoint:
    def test_calls_wg_set_with_correct_args(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("airvpn_picker.wg.subprocess.run", return_value=completed) as run:
            set_endpoint(
                interface="wg2",
                peer_pubkey=PEER_KEY,
                ip="37.46.199.66",
                port=1637,
            )
        run.assert_called_once_with(
            [
                DEFAULT_WG_BINARY,
                "set",
                "wg2",
                "peer",
                PEER_KEY,
                "endpoint",
                "37.46.199.66:1637",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_brackets_ipv6(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("airvpn_picker.wg.subprocess.run", return_value=completed) as run:
            set_endpoint(
                interface="wg2",
                peer_pubkey=PEER_KEY,
                ip="2001:db8::1",
                port=1637,
            )
        called_args = run.call_args.args[0]
        assert "[2001:db8::1]:1637" in called_args

    def test_raises_on_failure(self) -> None:
        with (
            patch(
                "airvpn_picker.wg.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "wg", stderr="permission"),
            ),
            pytest.raises(WgCommandError, match="permission"),
        ):
            set_endpoint(
                interface="wg2",
                peer_pubkey=PEER_KEY,
                ip="1.2.3.4",
                port=1637,
            )

    def test_dry_run_skips_subprocess(self) -> None:
        with patch("airvpn_picker.wg.subprocess.run") as run:
            set_endpoint(
                interface="wg2",
                peer_pubkey=PEER_KEY,
                ip="1.2.3.4",
                port=1637,
                dry_run=True,
            )
        run.assert_not_called()

    def test_invalid_ip_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="invalid endpoint IP"):
            set_endpoint(
                interface="wg2",
                peer_pubkey=PEER_KEY,
                ip="not-an-ip",
                port=1637,
            )


class TestValidatePubkey:
    def test_accepts_valid(self) -> None:
        validate_pubkey(PEER_KEY)

    def test_rejects_wrong_length(self) -> None:
        with pytest.raises(ValueError, match=str(PEER_PUBKEY_LEN)):
            validate_pubkey("short")

    def test_rejects_non_base64(self) -> None:
        bad = "!" * PEER_PUBKEY_LEN
        with pytest.raises(ValueError, match="base64"):
            validate_pubkey(bad)

    def test_rejects_correct_length_but_wrong_decoded_size(self) -> None:
        # 44 characters of valid base64 that decode to a non-32-byte string.
        # "A" * 44 decodes to 33 bytes (44 * 6 / 8 = 33), so it passes the
        # length and base64 checks but fails the decoded-length check.
        bad = "A" * PEER_PUBKEY_LEN
        with pytest.raises(ValueError, match="32 bytes"):
            validate_pubkey(bad)
