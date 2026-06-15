# Findings: issue-31-cli-coverage

## Round 1

### Code Reviewer
- Blocking: 0
- Should Fix: 1
- Informational: 4
- Key Issues:
  - **Should Fix**: the `explain <unknown>` test asserted `"unknown probe" in result.output`, but
    that message is written to stderr (`err=True`); whether it lands in `result.output` depends on
    the click version's stream handling, and `pyproject` allows a wider click range than the
    locked 8.2.1. Made the assertion version-robust: key off the deterministic `exit_code == 1`
    and check `result.output` plus `result.stderr` (when stderr was captured separately).
  - Informational (verified, no change): the `--version` semver-regex approach is correct given
    `_resolve_version`'s installed-dist-vs-`__version__` fallback; `init` deliberately runs without
    a pre-created schema (the `count()==0` would raise if the table were missing); the `clear`
    abort test uses exit code + row count (the deterministic contract). `_nct_app` duplicates the
    existing `_make_app` helper — candidate for a shared `conftest.py` later.
- Judgment: Useful review — caught a genuine cross-version fragility; assertions are otherwise
  faithful. Addressed.

### Doc Parrot
- Divergences Found: 0
- Details: changed callables are test functions / helpers (no docstrings); no CLI code changed.
- Judgment: N/A.

### Actions taken (Round 1)
- `test_cli_commands.py`: made the unknown-probe message assertion robust across click's
  stdout/stderr stream handling.

### Carry-forward (informational, not changed)
- `_nct_app` / `_seed_one` duplicate helpers already present in `test_network_check_tool_fixes.py`;
  a shared `tests/conftest.py` fixture would dedupe the dashboard/CLI test apps. Deferred.
- No version bump: test-only change.
