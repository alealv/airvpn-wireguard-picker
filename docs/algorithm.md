# Selection algorithm

This document explains exactly how `airvpn-picker` decides whether to switch
the WireGuard endpoint and to what.

## Inputs

Per run, the picker has three inputs:

1. **Live AirVPN status** — fetched from `https://airvpn.org/api/status/`
   (no auth, ~200 KB JSON, refreshed every minute upstream).
2. **Current endpoint** — the IPv4 address `wg show <iface> endpoints`
   reports for the configured peer (or `None` if the peer has no
   endpoint set yet).
3. **Selector options** — geo allowlists, max load, hysteresis threshold,
   passed in via CLI flags.

## API fields used

For each server in the response we read the following fields:

| Field            | Type    | Used for                                          |
| ---------------- | ------- | ------------------------------------------------- |
| `public_name`    | string  | Identification + logging                          |
| `country_code`   | string  | Geo filter (`--allowed-countries`)                |
| `continent`      | string  | Geo filter (`--allowed-continents`)               |
| `health`         | string  | Filter (`ok` / `warning` / `error`)               |
| `currentload`    | int     | Primary ranking key + filter (`--max-load`)       |
| `users`          | int     | First tie-break                                   |
| `bw`             | int     | Second tie-break                                  |
| `ip_v4_in1..in4` | strings | Endpoint candidates (we use `ip_v4_in1` to set)   |

Other fields like `bw_max`, `country_name`, `location`, `ip_v6_*`, and
`warning` are parsed but not yet used in selection. Servers missing any
required field are dropped at parse time.

## Step 1: filter

A server is a **candidate** if and only if all of the following are true:

- `health == "ok"` (so `warning` and `error` are excluded)
- `currentload <= --max-load` (default 80)
- Geo:
  - if `--allowed-countries` is non-empty, `country_code` (lowercased)
    is in the allowlist
  - otherwise, `continent` is in `--allowed-continents` (default `Europe`)
- `ip_v4_in1` is a parseable IPv4 address

If no servers pass the filter the picker exits with code `2`
(`EXIT_NO_CANDIDATES`) and logs an error. This is a configuration
problem (over-restrictive flags) or, much rarer, an AirVPN-wide outage.

## Step 2: rank

Surviving candidates are sorted by a strict ascending tuple:

```
(currentload, users, bw)
```

The first element is the dominant ranking key. The other two are
deterministic tie-breakers so that two runs against the same data
always produce the same winner.

Why these tie-breakers?

- **`currentload`** is the load percentage AirVPN reports for the
  server. Lower means more headroom. This is the field that
  correlates most strongly with available throughput in practice.
- **`users`** as a secondary key prefers a less-busy server when load
  is identical (e.g. both at 0% or both at 5%).
- **`bw`** as a tertiary key, *ascending*, prefers a server that is
  currently moving less aggregate traffic — which is correlated with
  recent low load.

The first element of the sorted list is the **winner**.

## Step 3: decide

Given the winner and the current live endpoint, the picker chooses one
of two actions: `switch` or `noop`. The decision tree is:

```
                   ┌─ no current endpoint ──────────────────► switch  (no-current)
                   │
   current ip ─────┼─ matches one of winner.ip_v4_in1..in4 ─► noop    (already-on-winner)
                   │
                   ├─ not in any candidate's IP set ────────► switch  (current-unhealthy)
                   │
                   └─ in candidate set:
                          delta = current.load − winner.load
                          if delta < hysteresis_pp:
                              ► noop  (below-hysteresis)
                          else:
                              ► switch  (load-improvement)
```

### Why the "current matches winner" check looks at all four IPs

Each AirVPN server publishes up to four entry IPs (`ip_v4_in1`..`in4`).
We only ever **set** `ip_v4_in1`, but the live tunnel may have ended up
on any of the four because of past DNS resolutions or manual configuration.
If the current endpoint matches *any* of the winner's IPs we treat that as
"already on the winner" and do nothing. This avoids a pointless swap that
would change the IP without changing the underlying server.

### Why the "not in any candidate's IP set" branch force-switches

The candidate set has already been filtered by `health == ok` and
`currentload <= max_load`. If the current endpoint IP is not in any
candidate's IP set, the tunnel is pointed at a server that is *either*:

- unhealthy (`warning` or `error`), or
- overloaded (`currentload > max_load`), or
- not advertised by AirVPN at all (deprecated/decommissioned IP).

In any of those cases, hysteresis is the wrong protection — we *want*
to leave immediately, not wait for the load delta to climb 15 points.

### Hysteresis

`--hysteresis-pp` (default `15`) is the minimum load improvement, in
percentage points, required to switch. Without it the picker would
flap every cycle: load values drift by single percentage points all
the time, and a 30-minute cron would happily rotate endpoints 48
times per day for cosmetic reasons. Each rotation costs an
unnecessary 5–10 second handshake delay on every flow that needs to
re-handshake.

A 15 pp threshold is the conservative default: a switch only happens
if the new server is *meaningfully* better. You can tighten it with
`--hysteresis-pp 10` if you have a low-traffic tunnel and aggressive
optimization matters more than stability, or relax it with
`--hysteresis-pp 25` if you want even more stability than the default.

## Step 4: act

If the decision is `switch`, the picker runs:

```
wg set <iface> peer <pubkey> endpoint <winner.ip_v4_in1>:<port>
```

This is **live and atomic**. It updates the kernel WireGuard state in
place: the interface is not restarted, no peers are reloaded, and any
existing TCP connections survive transparently because WireGuard simply
re-handshakes against the new endpoint on the next packet that needs to
be encrypted. There is no explicit downtime window.

If the decision is `noop`, no `wg` command is run.

In both cases, the picker writes one JSON line to the log file and
overwrites the state file with the latest decision.

## Step 5: persist

Two files are written on every non-dry-run invocation:

- **State file** (`--state-file`, default `/var/db/airvpn-picker.json`):
  a single JSON object with the last decision. Used so that future
  observability tools can answer "when did we last switch?" without
  parsing the full log.

- **Log file** (`--log-file`, default `/var/log/airvpn-picker.log`):
  one JSON object per line, appended forever. Each entry contains:

  ```json
  {
    "ts": 1727190123.456,
    "action": "switch",
    "reason": "load-improvement",
    "winner":  {"name": "Adhil",  "country": "de", "load": 26, "ip": "37.46.199.66"},
    "current": {"name": "Achird", "ip": "185.156.175.34", "load": 95},
    "candidates_count": 118
  }
  ```

  Rotate this file with the standard FreeBSD `newsyslog` mechanism if
  it grows too large; the picker itself does no rotation.

## Why not measure throughput?

We could measure live throughput from the picker host and pick by that
instead of by load, but it has serious downsides:

- Each measurement costs bandwidth and time — testing every European
  candidate every 30 minutes is wasteful and disruptive.
- Switching the endpoint to test it tears down the current measurement
  baseline.
- AirVPN's `currentload` field is a server-side metric that already
  reflects what matters: how saturated the upstream side is. In
  practice, low load very strongly correlates with high downstream
  throughput.

If real throughput measurement turns out to matter for your use case,
file an issue with a concrete reproducer.

## Why use `ip_v4_in1` and not the FQDN?

Two reasons:

1. **Avoids DNS races at boot.** WireGuard re-resolves peer FQDNs
   only when the tunnel comes up, and on systems like OPNsense the
   DNS resolver may not be ready yet. Using a raw IP completely
   sidesteps this whole class of bug.

2. **Determinism.** AirVPN's FQDNs (`ch3.vpn.airdns.org`,
   `nl.vpn.airdns.org`, etc.) often resolve to several rotating IPs.
   The picker would be choosing one server but actually pointing
   the tunnel at a different one if the resolver picked a stale or
   newer entry. Using the IP from the API directly removes that gap.
