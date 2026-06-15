# Plan: issue #31 — CLI command coverage (M10 phase 4)

## Goal (verbatim from issue #31)

> Both command-line entry points have unit tests for their core commands, closing the last named
> M10 coverage gap (the two CLIs).

## Approach

New `tests/test_cli_commands.py` using `click`'s `CliRunner`. Characterization tests — no runtime
change.

### netdiag CLI (`from netdiag.cli import cli`) — no DB / network needed

- `--version` → exit 0, output matches a version pattern (don't pin the number: `_resolve_version`
  returns the installed dist version or the in-tree `__version__`, which differs between CI and a
  source checkout).
- `--help` → exit 0, lists commands (`explain`, `list-probes`).
- `list-probes` → exit 0, includes a known probe (`gateway`) and the layer format.
- `explain` (no arg) → exit 0, "Available checks".
- `explain gateway` → exit 0, contains "What:" and "Why:".
- `explain <unknown>` → exit 1, "unknown probe".

### network_check_tool CLI (`network-checker`) — temp DB

Mirror the existing pattern in `test_network_check_tool_fixes.py`: build a Flask app bound to a
tmp SQLite DB and `monkeypatch.setattr(cli_module, "create_app", lambda *a, **k: app)` so the
command runs against it.

- `init` → exit 0, "initialized successfully"; the `PingResult` table now exists (a `count()`
  inside an app context returns 0 rather than raising).
- `clear` with `input="y\n"` → exit 0, "All ping data cleared", seeded rows removed.
- `clear` with `input="n\n"` → non-zero (aborted), seeded row retained.

## Out of scope

- Long-running commands (`run-server`, `start-monitoring`) and network commands (`check`, `ping`,
  `monitor`, `watch`, `peer`); `check --help`/`--jobs` is already covered by #20. No CLI behaviour
  changes.

## Versioning

Test-only → no version bump / CHANGELOG entry (as in #25/#27/#29).
