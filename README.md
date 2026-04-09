# airvpn-wireguard-picker

Pick the fastest [AirVPN](https://airvpn.org/) WireGuard server from their public
status API and update a live `wg` peer endpoint — without restarting the tunnel.

> **Status:** Early development. APIs and CLI flags may change before `v1.0.0`.

## Why

AirVPN issues a single WireGuard public key per account: every server you add as
a "peer" collapses into one active endpoint, and there is no built-in failover or
load balancing. If the server you happened to handshake with becomes congested,
your throughput tanks and you have to switch manually.

This tool runs on a schedule (cron, systemd timer, or OPNsense configd action),
queries `https://airvpn.org/api/status/`, applies a configurable filter
(continent / countries / max load / health), picks the least-loaded healthy
server, and atomically updates the peer endpoint via `wg set`. Active TCP
connections survive the swap because the interface is never restarted.

It uses the **raw IPv4 address** from the API response, not the FQDN, which
also sidesteps DNS-race-at-boot issues that affect WireGuard on systems where
the resolver isn't ready before the tunnel comes up.

## Features

- Zero runtime dependencies — Python 3.11 stdlib only
- Hysteresis to prevent tunnel thrashing on minor load drift
- Dry-run mode for safe inspection
- Structured JSON logging
- Designed for OPNsense / FreeBSD but works on any Linux WireGuard host

## Quickstart

```bash
uv run airvpn-picker --help
```

Full documentation lives in [`docs/`](./docs):

- [`docs/algorithm.md`](docs/algorithm.md) — selection logic and hysteresis details
- [`docs/opnsense.md`](docs/opnsense.md) — OPNsense install + configd + cron walkthrough

## Alternatives considered

| Project | Approach | Why this tool exists anyway |
|---|---|---|
| [AirVPN Eddie](https://github.com/AirVPN/Eddie) | Official GUI client, has internal server rating | Heavy GUI app, not deployable on a headless OPNsense VM |
| [zimbabwe303/WireGuard-rotate-AirVPN](https://github.com/zimbabwe303/WireGuard-rotate-AirVPN) | Random rotation through a server list | Random ≠ fastest; rewrites `.conf` files instead of using `wg set` |
| [FingerlessGlov3s/OPNsensePIAWireguard](https://github.com/FingerlessGlov3s/OPNsensePIAWireguard) | Health-check + rotate for PIA | PIA-specific, not AirVPN |

## License

[MIT](./LICENSE)
