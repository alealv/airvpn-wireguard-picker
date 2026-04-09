# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
