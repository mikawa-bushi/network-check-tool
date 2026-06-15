# Plan: issue #25 — pytest in CI + probe-parser coverage (M10 phase 1)

## Goal (verbatim from issue #25)

> The automated test suite runs in CI on every push and pull request (today CI only checks Black
> formatting), so regressions are caught automatically — and the most glaring coverage gap the
> review called out, the probe **output parsers**, gets unit tests.

## Approach

### 1. Add a pytest CI job

New workflow `.github/workflows/tests.yml` (kept separate from the Black workflow so each check is
independently legible), mirroring the Black workflow's toolchain: `actions/checkout`,
`setup-python` at **3.10** (matches the existing Black job and the `^3.10` floor in `pyproject`),
`snok/install-poetry`, the same `.venv` cache, then `poetry install` (main + dev, so Flask/netdiag
deps **and** pytest are present) and `poetry run pytest`. Triggers on push and PR for all branches,
like the Black workflow.

### 2. Make the suite pass on the CI Python (3.10)

`tests/test_pingservice_netdiag.py` currently does `import tomllib`, which is **3.11+ only** — on
the CI's 3.10 it would error. Replace the `tomllib` parse of `pyproject.toml` with a regex text
scan (`^\s*ping3\s*=`), which needs no TOML parser and works on any Python. (This is the only
3.11-only usage in the suite — verified by grep.)

### 3. Probe-parser unit tests — `tests/test_probe_parsers.py`

Cover the two untested output parsers the doc names, by feeding fixed command output (no real
network/subprocess):

- `traceroute._parse_hops(text)` — a pure function. Assert a normal multi-hop trace parses to the
  expected `hop/ip/rtt_ms/timeout` records (including a `* * *` timeout hop), and that a hop which
  first times out then resolves is "upgraded" to the resolved entry (the documented branch).
- `icmp.ping(host)` — stub `netdiag.probes.icmp.run` to return a canned `CommandResult` and pin
  `is_windows()` to `False`; assert a success transcript yields `(avg_rtt_ms, 0.0, text)` and a
  100%-loss transcript yields `(None, 100.0, text)`.

## Versioning / CHANGELOG

No runtime/package code changes — only a CI workflow, new tests, and a test-only fix. So **no
package version bump**; add a short note to `netdiag/CHANGELOG.md` under an explicit
"tooling/tests" framing (or skip if it muddies the per-version log). Justified in the findings.

## Alternatives considered / rejected

- **Run CI on 3.11 to keep `tomllib`**: rejected — the project declares `^3.10` and the Black job
  uses 3.10; CI should validate the supported floor, and the `tomllib` dependency is avoidable.
- **Parse `pyproject.toml` with the `toml` dep**: workable (it's a dependency) but a regex scan is
  simpler, needs nothing installed, and the assertion ("ping3 is not a declared dep") is naturally
  a line-level check.
- **Extract `icmp`'s inline parser into a pure helper**: deferred — testing through `ping()` with a
  stubbed `run` covers it without touching probe source.

## Out of scope

- Broad coverage of both CLIs, `report` rendering, and the dashboard View layer (later M10 phases).
- Any change to runtime behaviour.

## Testing the change itself

Run the full suite locally (green), confirm Black clean, and confirm on the PR that the new
"Run pytest" check executes on Python 3.10 and passes — that CI run is the real 3.10 validation.
