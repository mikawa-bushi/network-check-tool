# Findings: issue-25-pytest-ci-probe-parsers

## Round 1

### Code Reviewer
- Blocking: 0
- Should Fix: 2
- Informational: 5
- Key Issues:
  - **Should Fix**: `tests.yml` shared the Black workflow's `.venv` cache key, but Black installs
    `--only=dev`; a cache hit could restore a `.venv` missing the main-group deps. Gave the tests
    job a distinct key (`venv-tests-…`).
  - **Should Fix**: `test_ping_parses_total_loss` didn't assert `raw == stdout` (the stdout/stderr
    fallback was untested in the loss path). Added the assertion.
  - **Informational (acted on)**: no `[tool.pytest.ini_options]` meant discovery defaulted to the
    repo root. Added `testpaths = ["tests"]` to keep CI from scanning anything but the suite.
  - Informational (verified, no change): the RTT regex deliberately captures only min/avg/max
    (group(2)=avg), so `rtt == 13.456` is faithful; the tomllib→regex scan correctly excludes TOML
    comment lines.
- Judgment: Accurate, useful review. No correctness bugs; all actionable items addressed.

### Doc Parrot
- Divergences Found: 0
- Details: no changed callable carries a docstring — the additions are test functions (out of
  scope by parrot rules) and a docstring-less `_canned` helper; the parsers under test
  (`_parse_hops`, `icmp.ping`) were not modified.
- Judgment: Nothing to review.

### Actions taken (Round 1)
- `tests.yml`: distinct `.venv` cache key.
- `test_probe_parsers.py`: assert `raw == stdout` in the total-loss test.
- `pyproject.toml`: `[tool.pytest.ini_options] testpaths = ["tests"]`.

### Versioning note
No runtime/shippable code changed — only a CI workflow, new tests, a test-only 3.10-compat fix,
and pytest config. So **no package version bump and no CHANGELOG entry** (the per-version CHANGELOGs
would be muddied by a tooling-only line). The real Python-3.10 validation is the PR's own new
"Run pytest" check.
