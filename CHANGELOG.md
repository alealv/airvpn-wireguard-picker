# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-05-07

Closes the loop on penalty tracking introduced in v0.2.0. Until now the
penalty term in the Eddie score was always 0 — the state machinery
existed but nothing incremented it. This release wires the obvious
trigger: a switch that doesn't produce a fresh handshake within a few
seconds is the strongest possible signal that the destination IP is
broken (firewall, dead server, AirVPN-side maintenance).

### Added

- `wg.show_latest_handshake(interface, peer_pubkey)` — reads
  `wg show <iface> latest-handshakes` and returns the unix epoch of
  the most recent successful handshake (0 if never).
- CLI orchestrates the post-switch verification automatically:
  before `set_endpoint`, snapshot the handshake epoch; after, sleep
  `--post-switch-wait` seconds (default 30s, just over WireGuard's
  25s handshake interval) and re-read. If the epoch did not advance,
  call `state.increment_penalty(ip)`. The penalty persists in the
  state file with 6h decay so the next picker run de-prefers that
  server by 1000 score points (default `--penalty-factor`).
- New CLI flags: `--post-switch-check / --no-post-switch-check`
  (default ON), `--post-switch-wait <seconds>`.

### Changed

- `cli.main` now imports `increment_penalty` and `show_latest_handshake`
  alongside the existing helpers; no breaking API changes vs v0.2.

## [0.2.0] - 2026-05-07

Eddie-compatible scoring with real ICMP ping measurement. The v0.1.0 picker
ranked servers solely by AirVPN-reported `currentload`, which doesn't
capture the path latency from your egress to the AirVPN edge. Servers with
"low load" but a slow Berlin→Frankfurt hop went undetected and got stuck.

This release adopts the scoring formula used by AirVPN's official Eddie
client (`Lib.Core/ConnectionInfo.cs::Score()`):

    score = ping_ms × W_ping
          + load_pct × W_load
          + users_pct × W_users
          + scorebase
          + penalty × W_penalty

with `speed` and `latency` modes that mirror Eddie's two presets. Lower
score wins. Hysteresis now applies in score space, not load space.

### Added

- `airvpn_picker.probe` — parallel ICMP ping via `/sbin/ping`. FreeBSD and
  Linux compatible. Returns median RTT in ms; -1 on unreachable.
- `airvpn_picker.scoring` — pure Eddie-compatible scoring with tunable
  per-mode factors.
- State file now caches per-IP ping EWMA (TTL 600s, alpha 0.3) so the
  picker doesn't re-probe every server on every cron tick, and tracks
  per-IP penalties with 6h decay for future post-switch handshake checks.
- CLI flags: `--probe-ping/--no-probe-ping`, `--ping-count`,
  `--ping-timeout`, `--ping-cache-ttl`, `--score-mode {speed,latency}`,
  `--ping-factor`, `--load-factor`, `--users-factor`, `--penalty-factor`,
  `--hysteresis-score`.
- `Server` now exposes `users_max`, `scorebase`, and a `users_pct`
  property — required to compute Eddie's full formula.
- `contrib/build-pyz.sh` for reproducible zipapp builds.

### Changed

- **Breaking**: `selector.decide` now requires `ping_lookup` (and accepts
  `penalty_lookup`) as keyword-only arguments.
- **Breaking**: `--hysteresis-pp` was removed in favour of
  `--hysteresis-score` (default 15 in score space). The old flag does not
  alias.
- **Breaking**: `Decision.reason` enum value `"load-improvement"` was
  renamed to `"score-improvement"` to reflect the metric change.
- `Decision` carries `winner_score`, `winner_ping_ms`, and
  `current_score`; the JSON log now includes them.
- The CLI probes ICMP only for servers that pass the geo+health+load
  filter (plus the current endpoint), not the entire server pool.

## [0.1.0] - 2026-04-09

### Fixed

- `wg.set_endpoint` now uses peer remove + re-add instead of a naked
  `wg set ... endpoint`. WireGuard's automatic endpoint roaming (per
  `wg(8)`) was reverting the new endpoint within 1–3 seconds when an
  existing authenticated session was active. The remove + re-add sequence
  destroys the session keys so the new endpoint holds permanently.
  The PSK, allowed-ips, and persistent-keepalive are read from the kernel
  via `wg show <iface>` — no extra CLI flags or config files needed.
- `selector.decide` now force-switches when the current server is in a
  disallowed geo (country or continent), not only when it is unhealthy or
  overloaded. Previously, a geo-excluded server could trigger hysteresis
  and block the switch.

### Added

- Project skeleton with `uv`, `ruff`, `ty`, `pytest`, `pre-commit`, GitHub
  Actions CI on Python 3.11 and 3.12.
- `airvpn_picker.api` — stdlib AirVPN status API client and `Server`
  dataclass. Drops servers missing required fields or with unparseable
  primary IPv4. 100% covered.
- `airvpn_picker.selector` — pure filter + lowest-load winner with strict
  tie-breaks (`currentload`, then `users`, then `bw`) and a hysteresis
  threshold to prevent tunnel thrashing.
- `airvpn_picker.wg` — `wg show` / `wg set` subprocess wrapper with
  IPv4/IPv6 endpoint parsing and base64 pubkey validation.
- `airvpn_picker.state` — JSON state file for the last decision plus
  append-only JSONL run log.
- `airvpn_picker.cli` — argparse entry point with explicit exit codes
  for API failure, no candidates, wg failure, and bad arguments.
- `contrib/actions_airvpnpicker.conf` — OPNsense configd action template.
- `contrib/install-opnsense.sh` — idempotent POSIX sh installer for
  OPNsense (shellcheck-clean).
- `docs/algorithm.md` — full selection algorithm reference.
- `docs/opnsense.md` — OPNsense install, GUI cron walkthrough,
  troubleshooting, uninstall.
- 60 unit tests with 96% line coverage. Real fixture captured from the
  live AirVPN status API (255 servers).
- MIT License.
