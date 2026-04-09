"""Persistent state file and structured run logging.

The state file records the picker's last decision so future runs can answer
"have I already switched recently?" without re-querying the live tunnel. The
log file holds one JSON object per run for human inspection and post-mortems.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from airvpn_picker.selector import Decision

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StateRecord:
    """Snapshot persisted between runs."""

    timestamp: float
    winner_name: str
    winner_ip: str
    winner_load: int
    action: str
    reason: str


def load_state(path: Path) -> StateRecord | None:
    """Read the last persisted decision, or None if the file is missing or invalid."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("could not read state file %s: %s", path, exc)
        return None
    try:
        return StateRecord(**data)
    except TypeError as exc:
        logger.warning("state file %s is malformed: %s", path, exc)
        return None


def save_state(path: Path, decision: Decision) -> None:
    """Persist the decision so the next run can compare against it."""
    record = StateRecord(
        timestamp=time.time(),
        winner_name=decision.winner.public_name,
        winner_ip=decision.endpoint_ip,
        winner_load=decision.winner.currentload,
        action=decision.action,
        reason=decision.reason,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(record), indent=2) + "\n")


def append_log(path: Path, decision: Decision) -> None:
    """Append a single JSON line describing this run to `path`."""
    entry = {
        "ts": time.time(),
        "action": decision.action,
        "reason": decision.reason,
        "winner": {
            "name": decision.winner.public_name,
            "country": decision.winner.country_code,
            "load": decision.winner.currentload,
            "ip": decision.endpoint_ip,
        },
        "current": {
            "ip": decision.current_endpoint_ip,
            "name": decision.current_server.public_name if decision.current_server else None,
            "load": decision.current_server.currentload if decision.current_server else None,
        },
        "candidates_count": decision.candidates_count,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
