"""Parallel ICMP ping probe for candidate AirVPN servers.

The picker's selection metric of choice is Eddie's score formula, which
needs a real ping_ms reading per candidate (not just the load% reported
by the API). This module shells out to /sbin/ping in parallel threads
and returns the median RTT for each IP, or -1 on timeout / unreachable.

Why subprocess instead of raw sockets: ICMP raw sockets need CAP_NET_RAW
on Linux and root on FreeBSD. /sbin/ping is already setuid in both, so
shelling out is the boring portable choice.
"""

from __future__ import annotations

import logging
import re
import statistics
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

DEFAULT_PING_COUNT = 3
DEFAULT_PING_TIMEOUT_S = 2.0
DEFAULT_MAX_WORKERS = 16

UNREACHABLE_PING_MS = -1.0

_RTT_RE = re.compile(r"time[=<]\s*([0-9.]+)\s*ms", re.IGNORECASE)


def ping_one(
    ip: str,
    count: int = DEFAULT_PING_COUNT,
    timeout_s: float = DEFAULT_PING_TIMEOUT_S,
) -> float:
    """Ping a single IP `count` times; return median RTT in ms.

    Returns ``UNREACHABLE_PING_MS`` (-1.0) on timeout, non-zero exit, or
    if no replies were captured. Never raises.
    """
    # -c / -W work on both FreeBSD and Linux iputils, with different units
    # for -W: BSD takes ms, Linux takes seconds. We rely on subprocess
    # timeout instead and only pass -c, which is universal.
    cmd = ["/sbin/ping", "-c", str(count), "-q", ip]
    deadline_s = max(timeout_s * count + 1.0, 3.0)
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=deadline_s,
            check=False,
        )
    except FileNotFoundError:
        # Some Linux distros put ping at /usr/bin/ping
        cmd[0] = "/usr/bin/ping"
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=deadline_s,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            logger.debug("ping unavailable or timed out for %s", ip)
            return UNREACHABLE_PING_MS
    except subprocess.TimeoutExpired:
        logger.debug("ping timed out for %s", ip)
        return UNREACHABLE_PING_MS

    if completed.returncode != 0:
        logger.debug("ping %s rc=%d", ip, completed.returncode)
        return UNREACHABLE_PING_MS

    rtts = [float(m) for m in _RTT_RE.findall(completed.stdout)]
    if not rtts:
        # Some pings only print summary; try the summary line.
        # FreeBSD: "round-trip min/avg/max/stddev = 1.5/2.0/2.5/0.4 ms"
        # Linux:   "rtt min/avg/max/mdev = 1.5/2.0/2.5/0.4 ms"
        summary = re.search(r"min/avg/max(?:/[a-z]+)?\s*=\s*[0-9.]+/([0-9.]+)/", completed.stdout)
        if summary:
            return float(summary.group(1))
        return UNREACHABLE_PING_MS

    return statistics.median(rtts)


def ping_many(
    ips: list[str],
    count: int = DEFAULT_PING_COUNT,
    timeout_s: float = DEFAULT_PING_TIMEOUT_S,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict[str, float]:
    """Ping each IP in parallel; return {ip: ping_ms}.

    Unreachable IPs map to ``UNREACHABLE_PING_MS`` (-1.0). Order of input
    is not preserved in iteration but the dict has every input IP.
    """
    if not ips:
        return {}

    results: dict[str, float] = {}
    workers = min(max_workers, len(ips))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(ping_one, ip, count, timeout_s): ip for ip in ips}
        for future in as_completed(futures):
            ip = futures[future]
            try:
                results[ip] = future.result()
            except Exception:
                logger.exception("unexpected ping failure for %s", ip)
                results[ip] = UNREACHABLE_PING_MS
    return results
