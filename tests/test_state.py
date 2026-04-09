"""Tests for state persistence and log appending."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from airvpn_picker.selector import Decision
from airvpn_picker.state import append_log, load_state, save_state
from tests.conftest import make_server


def make_decision(
    action: Any = "switch",
    reason: Any = "no-current",
) -> Decision:
    winner = make_server(
        name="Adhil",
        country="de",
        load=26,
        users=359,
        bw=500,
        bw_max=2000,
        ips=("37.46.199.66",),
    )
    return Decision(
        action=action,
        reason=reason,
        winner=winner,
        endpoint_ip="37.46.199.66",
        current_endpoint_ip=None,
        current_server=None,
        candidates_count=7,
    )


def test_load_state_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert load_state(tmp_path / "nope.json") is None


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    save_state(state_path, make_decision())

    loaded = load_state(state_path)
    assert loaded is not None
    assert loaded.winner_name == "Adhil"
    assert loaded.winner_ip == "37.46.199.66"
    assert loaded.action == "switch"


def test_load_state_handles_malformed_json(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_state(bad) is None


def test_load_state_handles_unexpected_schema(tmp_path: Path) -> None:
    bad = tmp_path / "schema.json"
    bad.write_text(json.dumps({"unexpected": "field"}))
    assert load_state(bad) is None


def test_save_state_creates_parent_dirs(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "nested" / "state.json"
    save_state(nested, make_decision())
    assert nested.exists()


def test_append_log_appends_one_jsonl_line(tmp_path: Path) -> None:
    log_path = tmp_path / "picker.log"
    append_log(log_path, make_decision())
    append_log(log_path, make_decision(action="noop", reason="already-on-winner"))

    lines = log_path.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["action"] == "switch"
    assert first["winner"]["name"] == "Adhil"
    assert second["action"] == "noop"
