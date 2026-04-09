# AGENTS.md

Guidance for AI coding agents (Claude Code, Codex, Cursor, Copilot, Devin, etc.)
working on `airvpn-wireguard-picker`. Humans are welcome to read this too.

> `CLAUDE.md` in this repo is a hardlink to this file. Edit either, both update.

## Project overview

A small command-line tool that:

1. Fetches the public AirVPN status API (`https://airvpn.org/api/status/`,
   no auth required)
2. Filters and ranks European WireGuard servers by load
3. Updates the live `wg` peer endpoint via `wg set` (no tunnel restart)

It is designed to run from cron on OPNsense/FreeBSD or any Linux WireGuard
host. The interesting failure mode it solves is that AirVPN issues a single
public key per account: every "peer" you add collapses into one active
endpoint, so there is no built-in failover or load balancing. This tool
provides both.

## Principles

These are non-negotiable. Read them before changing code.

### KISS — Keep It Simple, Stupid

- Prefer **the obvious solution** over the clever one. If a five-line
  if/else is clearer than a three-line conditional expression, write the
  if/else.
- **Don't add abstractions until they earn their keep.** A single concrete
  call site is not enough to justify a new helper. Two is borderline.
  Three or more is the threshold.
- **No premature configuration.** Every CLI flag costs ongoing
  maintenance and test surface. Resist the urge to add knobs nobody asks
  for.
- **No layered indirection without a reason.** No strategy patterns, no
  factories, no DI containers. The decision tree in `selector.decide()`
  is intentionally a flat sequence of `if` branches with early returns.
  Do not "improve" it.
- **Functions do one thing.** If a function name needs the word "and",
  split it.

### DRY — Don't Repeat Yourself

- Subprocess invocations, error wrapping, and validation logic must
  exist in **exactly one place**. See `wg._run_wg` as the canonical
  example.
- Test data builders live in `tests/conftest.py`, not duplicated in
  every test file. See `tests.conftest.make_server`.
- Constants are defined once at the top of their owning module
  (`DEFAULT_*`, `_NONE_LITERALS`, etc.) and imported by name where
  needed.
- Naming conventions are uniform across modules: `snake_case` for
  functions and variables, `PascalCase` for classes, `SCREAMING_SNAKE`
  for constants, leading underscore for private helpers.

### Zero runtime dependencies

This tool deploys to OPNsense, where every installed package is one
more thing that can break across upgrades. We use **stdlib only** at
runtime: `urllib.request`, `json`, `argparse`, `logging`, `subprocess`,
`pathlib`, `dataclasses`, `ipaddress`, `base64`, `re`.

If you find yourself wanting to add `requests`, `httpx`, `pydantic`, or
similar — **stop**. Solve it with the stdlib. If you genuinely cannot,
file an issue with the reasoning before adding the dependency.

Dev-only dependencies (ruff, ty, pytest, pre-commit) are fine and live
in `[dependency-groups.dev]`.

### Stable shapes for the cron loop

This program runs unattended every 30 minutes from cron. Any change
that introduces nondeterminism, flakiness, or possible runaway state
is a bug, not a feature. Specifically:

- Selection must be **deterministic** for a given API response. The
  ranking tuple `(currentload, users, bw)` is what makes it so. Do not
  introduce randomization, weighted RNG, or floating-point compares.
- The state file in `/var/db` and the log file in `/var/log` are
  appended to forever. Never auto-rotate from inside the picker —
  rotation belongs to `newsyslog` on FreeBSD and `logrotate` on Linux.
- Hysteresis (`--hysteresis-pp`, default 15) exists so that load drift
  doesn't cause endpoint thrashing. Lowering the default below ~10
  will cause flapping in production. Don't.

## Commands

All commands assume the repo root as cwd and `uv` installed.

```bash
# install dev deps
uv sync --dev

# run the picker against the live API in dry-run (no root needed)
uv run airvpn-picker --interface wg2 --peer-pubkey '<KEY>' --dry-run --log-level DEBUG

# full check sweep (run before every commit)
uv run ruff check .                 # lint
uv run ruff format --check .        # format
uv run ty check src tests           # type check (Astral's ty; falls back to pyright if needed)
uv run pytest -q --cov              # tests with coverage

# auto-fix lint and format issues
uv run ruff check . --fix
uv run ruff format .

# shell linting (only contrib/ has shell)
shellcheck contrib/install-opnsense.sh
```

The pre-commit hook runs ruff, shellcheck, shfmt, and end-of-file-fixer.

## Project structure

```
airvpn-wireguard-picker/
├── AGENTS.md                       ← you are here (CLAUDE.md is a hardlink)
├── README.md                       ← human-facing intro
├── CHANGELOG.md                    ← keep-a-changelog format
├── LICENSE                         ← MIT
├── pyproject.toml                  ← uv project, ruff/ty/pytest config
├── uv.lock                         ← locked dev deps (committed)
├── .pre-commit-config.yaml
├── .github/workflows/ci.yml        ← ruff + ty + pytest matrix on py3.11/3.12
├── src/airvpn_picker/
│   ├── __init__.py                 ← __version__
│   ├── __main__.py                 ← `python -m airvpn_picker` entrypoint
│   ├── api.py                      ← AirVPN status API client (urllib + json)
│   ├── selector.py                 ← pure filter + decision tree + hysteresis
│   ├── wg.py                       ← `wg show` / `wg set` subprocess wrapper
│   ├── state.py                    ← JSON state file + JSONL run log
│   ├── cli.py                      ← argparse glue, exit codes, orchestration
│   └── py.typed                    ← PEP 561 marker
├── tests/
│   ├── conftest.py                 ← shared fixtures and `make_server` builder
│   ├── fixtures/status_sample.json ← real 218 KB AirVPN API snapshot
│   └── test_*.py                   ← one test file per source module
├── contrib/
│   ├── actions_airvpnpicker.conf   ← OPNsense configd action template
│   └── install-opnsense.sh         ← idempotent POSIX sh installer (shellcheck-clean)
└── docs/
    ├── algorithm.md                ← full selection logic reference
    └── opnsense.md                 ← step-by-step OPNsense install + cron + troubleshooting
```

### Module responsibilities

| Module        | Responsibility                                              |
| ------------- | ----------------------------------------------------------- |
| `api.py`      | Fetch + parse the AirVPN status payload into `Server`s     |
| `selector.py` | **Pure functions** that decide what to do given inputs      |
| `wg.py`       | The only place that runs `subprocess`                       |
| `state.py`    | The only place that writes files                            |
| `cli.py`      | Argparse + glue. Maps exceptions to exit codes.             |

The split is deliberate: `selector.py` has **no I/O**, **no subprocess**,
**no file access**. That makes it trivially testable and lets us cover
every branch with synthetic data plus the real fixture.

## Code style

- **Python 3.11+** features are fine (`X | Y` unions, `match`, etc.).
- **Type hints everywhere**, including return types. `from __future__
  import annotations` is the first import in every module.
- **Docstrings** in Google style on every public module, class, and
  function. Private helpers (`_underscore_prefixed`) may omit them.
  Ruff enforces this via `D` rules with `convention = "google"`.
- **Dataclasses** are `frozen=True, slots=True` by default. Use
  `dataclass(frozen=True, slots=True)` unless you have a concrete
  reason not to.
- **Errors**: domain errors are custom exceptions (`AirVpnApiError`,
  `WgCommandError`, `NoCandidatesError`). They are raised from
  the layer that detects them and caught at the CLI boundary in
  `cli.main()`, where they map to exit codes.
- **Logging** uses `logging.getLogger(__name__)`. The CLI configures
  the root logger; library code never calls `basicConfig`.
- **Ruff** rule selection lives in `pyproject.toml`. Suppressions are
  rare and always have a comment. The current set is `E W F I B C4
  UP SIM RET PTH ARG PL RUF S N ANN D`. The two ignored rules are
  `PLR0913` (CLI orchestrators legitimately have many args) and `S603`
  (we use list-form subprocess calls, no shell).

### Things to avoid

- Don't catch `Exception`. Catch the specific type you can recover from.
- Don't use `os.path` — use `pathlib.Path`.
- Don't use `print()` for output. The CLI uses `logging` for all
  diagnostics; structured machine-readable output goes to the JSONL log
  file.
- Don't add `# type: ignore` without a comment. Don't add `# noqa`
  without a comment. Both should be vanishingly rare.
- Don't use `mypy`. We use `ty` (Astral's checker) with `pyright` as a
  fallback. They're configured in `pyproject.toml`.
- Don't introduce a config file format (YAML, TOML, INI for runtime
  settings). Every knob is a CLI flag.

## Testing

- Unit tests live in `tests/test_<module>.py`, one per source module.
- The shared `make_server` builder is in `conftest.py`. Use it; do not
  reimplement.
- For tests that need live-API-like data, use the `status_sample`
  pytest fixture (it loads `tests/fixtures/status_sample.json`).
- Subprocess and network calls are mocked at the boundary
  (`subprocess.run` for wg, `urlopen` for api). The selector layer is
  pure and needs no mocks.
- Coverage target is **≥95% line + branch**. The CI pipeline does not
  hard-fail on coverage drops, but the contributor SHOULD restore
  coverage before requesting review.
- The test suite must be **fast**: <1s wall clock for the full sweep.
  No sleeps, no real network, no real subprocess. If a test is slow,
  it's wrong.
- Test names are sentences: `test_picks_lowest_load`,
  `test_force_switch_when_current_unhealthy`. The test name should
  read as the assertion the test is making.

## Security considerations

- The picker runs **as root** on the deploy target (because `wg set`
  requires it). Treat its inputs accordingly.
- The only network input is the AirVPN status API. We treat it as
  untrusted: invalid JSON, missing fields, unparseable IPs, and HTTP
  errors all fail closed (the picker does nothing) rather than fail
  open (random `wg set` calls).
- Subprocess invocations are **always lists**, never strings. There is
  no shell involved. The `noqa: S603` on `_run_wg` is correct: bandit's
  warning targets shell-style invocations, which we don't use.
- The peer public key from CLI is validated by `wg.validate_pubkey`
  (length + base64 + decoded byte count) before being passed to
  subprocess.

## Release process

> Not yet automated. Manual for v0.1.0 onward; will be automated in v0.2.

1. Bump `__version__` in `src/airvpn_picker/__init__.py`.
2. Bump `version` in `pyproject.toml` to match.
3. Update `CHANGELOG.md` — move `[Unreleased]` to a dated version section.
4. Run the full check sweep (see Commands above) and confirm all green.
5. Commit: `chore: release vX.Y.Z`.
6. Tag: `git tag -a vX.Y.Z -m "vX.Y.Z"`.
7. Push: `git push origin main --tags`.
8. Build a zipapp:
   ```bash
   python -m zipapp src/airvpn_picker -m airvpn_picker.cli:main \
     -p '/usr/local/bin/python3.11' -o airvpn-picker-vX.Y.Z.pyz
   sha256sum airvpn-picker-vX.Y.Z.pyz > airvpn-picker-vX.Y.Z.pyz.sha256
   ```
9. Create the GitHub Release with the zipapp + checksum attached.

## Things I (the agent) must remember

- **Run the full check sweep before claiming a task is done.** Not
  partial. All four: `ruff check`, `ruff format --check`, `ty check`,
  `pytest -q --cov`.
- **Verify against real evidence.** If a test passes after a refactor,
  also run `airvpn-picker --dry-run` against the live API to confirm
  end-to-end behavior is unchanged.
- **Don't hide failures.** If a test starts failing for a real reason,
  fix the code, not the test. If a test starts failing for a bad
  reason, fix the test and explain why in the commit message.
- **Never modify the captured fixture** at `tests/fixtures/status_sample.json`
  unless you intend to validate the change. The fixture is a frozen
  point-in-time snapshot of the AirVPN API; tests that depend on
  specific server names rely on it being stable.
- **Update CHANGELOG.md** for any user-visible change. Don't update it
  for refactors that have no observable effect.
- **Read the plan first.** The original implementation plan lives at
  `~/.claude/plans/floating-wobbling-harp.md` (outside this repo, in
  Claude's plan store). It explains *why* design decisions were made.
