"""Tests for the CLI orchestration layer.

These tests stub out network and subprocess calls so the full main() flow can
run end-to-end against the real fixture.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from airvpn_picker.api import AirVpnApiError, parse_status
from airvpn_picker.cli import (
    EXIT_API_FAILURE,
    EXIT_BAD_ARGS,
    EXIT_NO_CANDIDATES,
    EXIT_OK,
    EXIT_WG_FAILURE,
    main,
)
from airvpn_picker.wg import WgCommandError

PEER_KEY = "PyLCXAQT8KkM4T+dUsOQfn+Ub3pGxfGlxkIApuig+hk="


@pytest.fixture
def base_argv(tmp_path: Path) -> list[str]:
    return [
        "--interface",
        "wg2",
        "--peer-pubkey",
        PEER_KEY,
        "--state-file",
        str(tmp_path / "state.json"),
        "--log-file",
        str(tmp_path / "picker.log"),
        "--log-level",
        "DEBUG",
    ]


def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as ei:
        main(["--help"])
    assert ei.value.code == 0


def test_invalid_pubkey_returns_bad_args(base_argv: list[str]) -> None:
    argv = base_argv.copy()
    idx = argv.index("--peer-pubkey")
    argv[idx + 1] = "tooshort"
    assert main(argv) == EXIT_BAD_ARGS


def test_full_run_dry_run(
    base_argv: list[str],
    status_sample: dict[str, Any],
    tmp_path: Path,
) -> None:
    base_argv.append("--dry-run")

    with (
        patch("airvpn_picker.cli.fetch_status", return_value=parse_status(status_sample)),
        patch("airvpn_picker.cli.show_current_endpoint_ip", return_value=None),
        patch("airvpn_picker.cli.set_endpoint") as mock_set,
    ):
        result = main(base_argv)

    assert result == EXIT_OK
    mock_set.assert_called_once()
    # In dry-run we should not have written state or log files.
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "picker.log").exists()
    # The set_endpoint stub itself was called with dry_run=True so the real wg binary
    # is never invoked even if the function were not mocked.
    assert mock_set.call_args.kwargs["dry_run"] is True


def test_full_run_writes_state_and_log(
    base_argv: list[str],
    status_sample: dict[str, Any],
    tmp_path: Path,
) -> None:
    with (
        patch("airvpn_picker.cli.fetch_status", return_value=parse_status(status_sample)),
        patch("airvpn_picker.cli.show_current_endpoint_ip", return_value=None),
        patch("airvpn_picker.cli.set_endpoint"),
    ):
        result = main(base_argv)

    assert result == EXIT_OK
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["winner_name"]
    log_lines = (tmp_path / "picker.log").read_text().splitlines()
    assert len(log_lines) == 1
    log = json.loads(log_lines[0])
    assert log["action"] == "switch"
    assert log["winner"]["ip"]


def test_api_failure_returns_correct_exit_code(base_argv: list[str]) -> None:
    with patch("airvpn_picker.cli.fetch_status", side_effect=AirVpnApiError("boom")):
        assert main(base_argv) == EXIT_API_FAILURE


def test_wg_show_failure_returns_correct_exit_code(
    base_argv: list[str],
    status_sample: dict[str, Any],
) -> None:
    with (
        patch("airvpn_picker.cli.fetch_status", return_value=parse_status(status_sample)),
        patch(
            "airvpn_picker.cli.show_current_endpoint_ip",
            side_effect=WgCommandError("nope"),
        ),
    ):
        assert main(base_argv) == EXIT_WG_FAILURE


def test_wg_set_failure_returns_correct_exit_code(
    base_argv: list[str],
    status_sample: dict[str, Any],
) -> None:
    with (
        patch("airvpn_picker.cli.fetch_status", return_value=parse_status(status_sample)),
        patch("airvpn_picker.cli.show_current_endpoint_ip", return_value=None),
        patch("airvpn_picker.cli.set_endpoint", side_effect=WgCommandError("perm")),
    ):
        assert main(base_argv) == EXIT_WG_FAILURE


def test_no_candidates_returns_correct_exit_code(
    base_argv: list[str],
    status_sample: dict[str, Any],
) -> None:
    base_argv += ["--allowed-countries", "xx"]  # no such country
    with (
        patch("airvpn_picker.cli.fetch_status", return_value=parse_status(status_sample)),
        patch("airvpn_picker.cli.show_current_endpoint_ip", return_value=None),
    ):
        assert main(base_argv) == EXIT_NO_CANDIDATES


def test_country_filter_picks_only_german_server(
    base_argv: list[str],
    status_sample: dict[str, Any],
    tmp_path: Path,
) -> None:
    base_argv += ["--allowed-countries", "de"]

    with (
        patch("airvpn_picker.cli.fetch_status", return_value=parse_status(status_sample)),
        patch("airvpn_picker.cli.show_current_endpoint_ip", return_value=None),
        patch("airvpn_picker.cli.set_endpoint") as mock_set,
    ):
        assert main(base_argv) == EXIT_OK

    # The winner from a DE-only filter must be a DE server.
    log = json.loads((tmp_path / "picker.log").read_text())
    assert log["winner"]["country"] == "de"
    mock_set.assert_called_once()
