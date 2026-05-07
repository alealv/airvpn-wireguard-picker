"""Persistent state file and structured run logging.

The state file records the picker's last decision so future runs can answer
"have I already switched recently?" without re-querying the live tunnel. It
also caches per-IP ping EWMA (so we don't pay the full ping fan-out on every
run when nothing meaningful changed) and per-IP penalties for servers that
failed to handshake after we switched to them. The log file holds one JSON
object per run for human inspection and post-mortems.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from airvpn_picker.selector import Decision

logger = logging.getLogger(__name__)

# How long a cached ping reading is treated as authoritative. After this we
# re-probe so the picker doesn't act on stale latency from hours ago.
PING_CACHE_TTL_S = 600.0
# EWMA weight for new readings. 0.3 ≈ "latest sample is 30%, history is 70%".
PING_EWMA_ALPHA = 0.3
# How many recent post-switch failures we keep tracking; older ones decay out.
PENALTY_DECAY_AFTER_S = 6 * 3600.0


@dataclass(frozen=True, slots=True)
class PingSample:
    """Cached ping reading for one IP."""

    ping_ms: float
    timestamp: float


@dataclass(frozen=True, slots=True)
class PenaltyRecord:
    """Per-IP penalty counter with last-touched timestamp for decay."""

    count: int
    last_touched: float


@dataclass(slots=True)
class StateRecord:
    """Snapshot persisted between runs.

    Older state files predate the ping cache and penalty fields; load_state
    backfills empty defaults so an upgrade is seamless.
    """

    timestamp: float
    winner_name: str
    winner_ip: str
    winner_load: int
    action: str
    reason: str
    ping_cache: dict[str, PingSample] = field(default_factory=dict)
    penalties: dict[str, PenaltyRecord] = field(default_factory=dict)


def load_state(path: Path) -> StateRecord | None:
    """Read the last persisted decision, or None if missing/invalid."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("could not read state file %s: %s", path, exc)
        return None

    return _hydrate_state(data)


def _hydrate_state(data: dict[str, Any]) -> StateRecord | None:
    try:
        ping_cache_raw = data.pop("ping_cache", {}) or {}
        penalties_raw = data.pop("penalties", {}) or {}
        ping_cache = {ip: PingSample(**v) for ip, v in ping_cache_raw.items()}
        penalties = {ip: PenaltyRecord(**v) for ip, v in penalties_raw.items()}
        return StateRecord(
            **data,
            ping_cache=ping_cache,
            penalties=penalties,
        )
    except TypeError as exc:
        logger.warning("state file is malformed: %s", exc)
        return None


def save_state(
    path: Path,
    decision: Decision,
    *,
    ping_cache: dict[str, PingSample] | None = None,
    penalties: dict[str, PenaltyRecord] | None = None,
) -> None:
    """Persist the decision and optional ping/penalty caches."""
    record = StateRecord(
        timestamp=time.time(),
        winner_name=decision.winner.public_name,
        winner_ip=decision.endpoint_ip,
        winner_load=decision.winner.currentload,
        action=decision.action,
        reason=decision.reason,
        ping_cache=ping_cache or {},
        penalties=penalties or {},
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    serializable = asdict(record)
    path.write_text(json.dumps(serializable, indent=2) + "\n")


def append_log(path: Path, decision: Decision, extra: dict[str, Any] | None = None) -> None:
    """Append a single JSON line describing this run to ``path``."""
    entry: dict[str, Any] = {
        "ts": time.time(),
        "action": decision.action,
        "reason": decision.reason,
        "winner": {
            "name": decision.winner.public_name,
            "country": decision.winner.country_code,
            "load": decision.winner.currentload,
            "ip": decision.endpoint_ip,
            "score": round(decision.winner_score, 2) if decision.winner_score is not None else None,
            "ping_ms": decision.winner_ping_ms,
        },
        "current": {
            "ip": decision.current_endpoint_ip,
            "name": decision.current_server.public_name if decision.current_server else None,
            "load": decision.current_server.currentload if decision.current_server else None,
            "score": (
                round(decision.current_score, 2) if decision.current_score is not None else None
            ),
        },
        "candidates_count": decision.candidates_count,
    }
    if extra:
        entry.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


# ── ping cache helpers ────────────────────────────────────────────────────────


def merge_ping_cache(
    cache: dict[str, PingSample],
    fresh: dict[str, float],
    now: float,
    alpha: float = PING_EWMA_ALPHA,
) -> dict[str, PingSample]:
    """Fold new ping readings into the cache via EWMA. Returns a new dict."""
    merged = dict(cache)
    for ip, ping_ms in fresh.items():
        if ping_ms < 0:
            # Don't poison the cache with unreachable readings; keep the
            # last known good value if we have one.
            continue
        prev = merged.get(ip)
        smoothed = ping_ms if prev is None else alpha * ping_ms + (1 - alpha) * prev.ping_ms
        merged[ip] = PingSample(ping_ms=smoothed, timestamp=now)
    return merged


def stale_ips(
    cache: dict[str, PingSample], now: float, ttl_s: float = PING_CACHE_TTL_S
) -> set[str]:
    """Return the set of IPs whose cached ping is older than ``ttl_s``."""
    return {ip for ip, sample in cache.items() if now - sample.timestamp > ttl_s}


def cached_ping(cache: dict[str, PingSample], ip: str) -> float:
    """Return the cached ping_ms for ``ip``, or -1 if absent."""
    sample = cache.get(ip)
    return sample.ping_ms if sample else -1.0


# ── penalty helpers ───────────────────────────────────────────────────────────


def decay_penalties(
    penalties: dict[str, PenaltyRecord],
    now: float,
    decay_after_s: float = PENALTY_DECAY_AFTER_S,
) -> dict[str, PenaltyRecord]:
    """Drop penalties older than ``decay_after_s``. Returns a new dict."""
    return {ip: rec for ip, rec in penalties.items() if now - rec.last_touched <= decay_after_s}


def increment_penalty(
    penalties: dict[str, PenaltyRecord],
    ip: str,
    now: float,
) -> dict[str, PenaltyRecord]:
    """Bump the penalty counter for ``ip``. Returns a new dict."""
    updated = dict(penalties)
    prev = updated.get(ip)
    updated[ip] = PenaltyRecord(
        count=(prev.count if prev else 0) + 1,
        last_touched=now,
    )
    return updated


def penalty_for(penalties: dict[str, PenaltyRecord], ip: str) -> int:
    """Return the current penalty count for ``ip``, or 0 if none."""
    rec = penalties.get(ip)
    return rec.count if rec else 0
