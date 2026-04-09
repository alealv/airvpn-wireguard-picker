"""Tests for the WireGuard subprocess wrapper."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from airvpn_picker.wg import (
    DEFAULT_ALLOWED_IPS,
    DEFAULT_PERSISTENT_KEEPALIVE,
    DEFAULT_WG_BINARY,
    PEER_PUBKEY_LEN,
    WgCommandError,
    _parse_tab_output,
    _read_peer_keepalive,
    parse_endpoint,
    parse_endpoints_output,
    set_endpoint,
    show_current_endpoint_ip,
    validate_pubkey,
)

PEER_KEY = "PyLCXAQT8KkM4T+dUsOQfn+Ub3pGxfGlxkIApuig+hk="
FAKE_PSK = "BAplilAyJY7PXGxhxRBPneIgkUt9KZPMDP/z7W+wSAc="


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


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


class TestParseTabOutput:
    def test_parses_psk(self) -> None:
        output = f"{PEER_KEY}\t{FAKE_PSK}\n"
        assert _parse_tab_output(output) == {PEER_KEY: FAKE_PSK}

    def test_none_literal_maps_to_empty(self) -> None:
        output = f"{PEER_KEY}\t(none)\n"
        assert _parse_tab_output(output) == {PEER_KEY: ""}

    def test_off_maps_to_empty(self) -> None:
        output = f"{PEER_KEY}\toff\n"
        assert _parse_tab_output(output) == {PEER_KEY: ""}

    def test_empty_output_returns_empty_dict(self) -> None:
        assert _parse_tab_output("") == {}
        assert _parse_tab_output("\n\n") == {}

    def test_malformed_line_skipped(self) -> None:
        # Line with no tab is silently skipped; valid lines still parsed.
        output = f"garbage-no-tab\n{PEER_KEY}\t{FAKE_PSK}\n"
        assert _parse_tab_output(output) == {PEER_KEY: FAKE_PSK}

    def test_whitespace_stripped_from_value(self) -> None:
        output = f"{PEER_KEY}\t  {FAKE_PSK}  \n"
        assert _parse_tab_output(output) == {PEER_KEY: FAKE_PSK}


class TestReadPeerKeepalive:
    def test_returns_integer_value(self) -> None:
        with patch(
            "airvpn_picker.wg.subprocess.run",
            return_value=_completed(f"{PEER_KEY}\t30\n"),
        ):
            assert _read_peer_keepalive("wg2", PEER_KEY, DEFAULT_WG_BINARY) == 30

    def test_returns_default_on_non_integer_value(self) -> None:
        # Should not raise; falls back to default.
        with patch(
            "airvpn_picker.wg.subprocess.run",
            return_value=_completed(f"{PEER_KEY}\tnot-a-number\n"),
        ):
            result = _read_peer_keepalive("wg2", PEER_KEY, DEFAULT_WG_BINARY)
            assert result == DEFAULT_PERSISTENT_KEEPALIVE


class TestShowCurrentEndpointIp:
    def test_returns_ip_for_known_peer(self) -> None:
        with patch(
            "airvpn_picker.wg.subprocess.run",
            return_value=_completed(f"{PEER_KEY}\t213.152.161.213:1637\n"),
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
            return_value=_completed(f"{PEER_KEY}\t(none)\n"),
        ):
            assert show_current_endpoint_ip(interface="wg2", peer_pubkey=PEER_KEY) is None

    def test_returns_none_when_peer_absent(self) -> None:
        with patch(
            "airvpn_picker.wg.subprocess.run",
            return_value=_completed("OTHERKEY=\t1.2.3.4:51820\n"),
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
    """set_endpoint does: read PSK + read allowed-ips + read keepalive + remove + readd."""

    def _make_side_effects(
        self,
        psk: str = FAKE_PSK,
        allowed_ips: str = "0.0.0.0/0 ::/0",
        keepalive: str = "25",
    ) -> list[subprocess.CompletedProcess[str]]:
        """Return mock subprocess.run return values for the 5 calls set_endpoint makes."""
        return [
            _completed(f"{PEER_KEY}\t{psk}\n"),  # wg show preshared-keys
            _completed(f"{PEER_KEY}\t{allowed_ips}\n"),  # wg show allowed-ips
            _completed(f"{PEER_KEY}\t{keepalive}\n"),  # wg show persistent-keepalive
            _completed(),  # wg set peer remove
            _completed(),  # wg set peer readd
        ]

    def test_calls_remove_then_readd(self) -> None:
        side_effects = self._make_side_effects()
        with patch("airvpn_picker.wg.subprocess.run", side_effect=side_effects) as run:
            set_endpoint(interface="wg2", peer_pubkey=PEER_KEY, ip="37.46.199.66", port=1637)

        calls = run.call_args_list
        # 3 reads + 1 remove + 1 readd = 5 total
        assert len(calls) == 5

        # Call 0: read preshared-keys
        assert calls[0].args[0] == [DEFAULT_WG_BINARY, "show", "wg2", "preshared-keys"]

        # Call 1: read allowed-ips
        assert calls[1].args[0] == [DEFAULT_WG_BINARY, "show", "wg2", "allowed-ips"]

        # Call 2: read persistent-keepalive
        assert calls[2].args[0] == [DEFAULT_WG_BINARY, "show", "wg2", "persistent-keepalive"]

        # Call 3: remove the peer
        assert calls[3].args[0] == [DEFAULT_WG_BINARY, "set", "wg2", "peer", PEER_KEY, "remove"]

        # Call 4: re-add with new endpoint (includes preshared-key <tmpfile>)
        readd_args = calls[4].args[0]
        assert readd_args[0] == DEFAULT_WG_BINARY
        assert "set" in readd_args
        assert "peer" in readd_args
        assert PEER_KEY in readd_args
        assert "allowed-ips" in readd_args
        assert "0.0.0.0/0,::/0" in readd_args
        assert "persistent-keepalive" in readd_args
        assert "25" in readd_args
        assert "endpoint" in readd_args
        assert "37.46.199.66:1637" in readd_args
        assert "preshared-key" in readd_args

    def test_readd_without_psk_when_peer_has_none(self) -> None:
        # When peer has no PSK, wg show returns "(none)" -> _parse_tab_output -> ""
        side_effects = self._make_side_effects(psk="(none)")
        with patch("airvpn_picker.wg.subprocess.run", side_effect=side_effects) as run:
            set_endpoint(interface="wg2", peer_pubkey=PEER_KEY, ip="1.2.3.4", port=1637)

        calls = run.call_args_list
        assert len(calls) == 5
        readd_args = calls[4].args[0]
        assert "preshared-key" not in readd_args

    def test_brackets_ipv6(self) -> None:
        side_effects = self._make_side_effects()
        with patch("airvpn_picker.wg.subprocess.run", side_effect=side_effects) as run:
            set_endpoint(interface="wg2", peer_pubkey=PEER_KEY, ip="2001:db8::1", port=1637)

        readd_args = run.call_args_list[4].args[0]
        assert "[2001:db8::1]:1637" in readd_args

    def test_raises_on_remove_failure(self) -> None:
        # Fail on the 4th call (remove)
        responses: list = [
            _completed(f"{PEER_KEY}\t{FAKE_PSK}\n"),
            _completed(f"{PEER_KEY}\t0.0.0.0/0 ::/0\n"),
            _completed(f"{PEER_KEY}\t25\n"),
            subprocess.CalledProcessError(1, "wg", stderr="permission denied"),
        ]
        with (
            patch("airvpn_picker.wg.subprocess.run", side_effect=responses),
            pytest.raises(WgCommandError, match="permission denied"),
        ):
            set_endpoint(interface="wg2", peer_pubkey=PEER_KEY, ip="1.2.3.4", port=1637)

    def test_dry_run_skips_subprocess(self) -> None:
        # dry_run should still call the 3 read commands to log what would happen,
        # but skip the destructive remove + readd calls.
        side_effects = self._make_side_effects()
        with patch("airvpn_picker.wg.subprocess.run", side_effect=side_effects) as run:
            set_endpoint(
                interface="wg2",
                peer_pubkey=PEER_KEY,
                ip="1.2.3.4",
                port=1637,
                dry_run=True,
            )
        # 3 reads only; remove + readd are skipped
        assert run.call_count == 3

    def test_dry_run_no_psk_skips_destructive_calls(self) -> None:
        # dry_run with no PSK: still 3 reads, no remove/readd.
        side_effects = self._make_side_effects(psk="(none)")
        with patch("airvpn_picker.wg.subprocess.run", side_effect=side_effects) as run:
            set_endpoint(
                interface="wg2",
                peer_pubkey=PEER_KEY,
                ip="1.2.3.4",
                port=1637,
                dry_run=True,
            )
        assert run.call_count == 3

    def test_invalid_ip_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="invalid endpoint IP"):
            set_endpoint(
                interface="wg2",
                peer_pubkey=PEER_KEY,
                ip="not-an-ip",
                port=1637,
            )

    def test_uses_default_allowed_ips_when_peer_has_none(self) -> None:
        side_effects = [
            _completed(f"{PEER_KEY}\t{FAKE_PSK}\n"),
            _completed(f"{PEER_KEY}\t(none)\n"),  # no allowed-ips
            _completed(f"{PEER_KEY}\t25\n"),
            _completed(),
            _completed(),
        ]
        with patch("airvpn_picker.wg.subprocess.run", side_effect=side_effects) as run:
            set_endpoint(interface="wg2", peer_pubkey=PEER_KEY, ip="1.2.3.4", port=1637)

        readd_args = run.call_args_list[4].args[0]
        assert DEFAULT_ALLOWED_IPS in readd_args

    def test_uses_default_keepalive_when_off(self) -> None:
        side_effects = [
            _completed(f"{PEER_KEY}\t{FAKE_PSK}\n"),
            _completed(f"{PEER_KEY}\t0.0.0.0/0 ::/0\n"),
            _completed(f"{PEER_KEY}\toff\n"),  # keepalive off
            _completed(),
            _completed(),
        ]
        with patch("airvpn_picker.wg.subprocess.run", side_effect=side_effects) as run:
            set_endpoint(interface="wg2", peer_pubkey=PEER_KEY, ip="1.2.3.4", port=1637)

        readd_args = run.call_args_list[4].args[0]
        assert str(DEFAULT_PERSISTENT_KEEPALIVE) in readd_args


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
