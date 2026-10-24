# Findings: issue-14-daemon-logging

## Round 1

### Code Reviewer
- Blocking: 0
- Should Fix: 3
- Informational: 6
- Key Issues:
  - `_setup_logging` had no idempotency guard → repeated calls stack handlers / leak a FileHandler.
  - The test fixture restored `lg.handlers` without `close()`ing removed handlers → FileHandler
    fd leak (ResourceWarning under `-W error`).
  - No direct test for the `Watcher` loop logging (plan listed it as an in-scope swallow site).
- Judgment: Strong review. Confirmed all four daemon swallow sites covered, the
  NullHandler/CLI-only-setup separation is idiomatic, `exc_info` in loops is safe, and that the
  narrow `except (OSError, JSONDecodeError)` in `status` was correctly left alone. All three
  Should-Fix addressed.

### Doc Parrot
- Divergences Found: 0
- Details:
  - `_setup_logging()` (netdiag/cli.py) — **OK**. Prose (quiet by default; verbose→DEBUG/stderr;
    log_file→file) matches the implementation. The added idempotency guard is additive safety,
    not a contract change.
  - `cli` group — docstring is a CLI tagline; no prose contract to verify.
  - Test callables → skipped.
- Judgment: No divergence.

### Actions taken (Round 1)
- Added an idempotency guard to `_setup_logging` (skip if a non-Null handler already attached).
- Test fixture now `close()`es any handler the test added before restoring (no fd leak;
  verified with `-W error::ResourceWarning`).
- Added `test_watcher_loop_logs_tick_failure` mirroring the Monitor test, closing the
  acceptance gap for the `Watcher` swallow site.

### Carry-forward (informational, not changed)
- Group `--verbose` vs `check`/`scenario` command-level `--verbose` operate at different parse
  positions (no collision); a future rename to `--log-level` could reduce overlap.
- Root `pyproject.toml` (0.2.0) and `netdiag.__version__` (0.1.5) are intentionally independent
  (`_resolve_version()` prefers distribution metadata).
