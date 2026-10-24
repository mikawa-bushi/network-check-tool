# Findings: issue-18-sqlite-wal-scheduler-pool

## Round 1

### Code Reviewer
- Blocking: 0
- Should Fix: 3
- Informational: 6
- Key Issues:
  - **Should Fix**: `SCHEDULER_MAX_WORKERS <= 0` crashed deep in `concurrent.futures`
    ("max_workers must be greater than 0") — no helpful message. Added an up-front
    `ValueError` guard in `SchedulerService.__init__` + a test.
  - **Should Fix**: `test_config_exposes_p3_tunables` asserted `0 < SCHEDULER_MAX_WORKERS < 20`,
    which fails if an operator exports a large value. Dropped the upper bound (the "small
    default" intent is pinned against `DEFAULT_SCHEDULER_MAX_WORKERS` in
    `test_scheduler_default_pool_is_small`, which is env-independent).
  - **Should Fix**: the two new env vars (`SQLITE_BUSY_TIMEOUT_MS`, `SCHEDULER_MAX_WORKERS`)
    were undocumented. Added them to the README Environment Variables section.
- Judgment: Strong review — no correctness bugs, but all three Should-Fix items were real
  robustness/usability gaps and have been addressed. Informational items (WAL PRAGMA result
  row not fetched, `check_same_thread` for file-backed SQLite, context-block timing) were
  verified non-issues and left as-is.

### Doc Parrot
- Divergences Found: 0
- Details:
  - Only one changed callable carries a docstring: `_register_sqlite_pragmas`
    (`network_check_tool/app.py:10`). `SchedulerService.__init__` is a dunder (out of scope);
    the nested `_set_sqlite_pragmas` listener has no docstring; `create_app`'s docstring is
    unchanged; test functions are out of scope.
  - Prose-derived description and usage example ran successfully and matched the prose's stated
    behaviour exactly (WAL, busy_timeout=5000, `False` for a non-SQLite stub engine). The
    "every new DBAPI connection" wording correctly conveys the future-only (post-registration)
    semantics, so no completeness gap.
- Judgment: Nothing to flag.

### Actions taken (Round 1)
- `SchedulerService.__init__`: raise `ValueError` for non-positive `max_workers`.
- `tests`: dropped env-sensitive upper bound; added `test_scheduler_rejects_non_positive_max_workers`.
- `README.md`: documented `SQLITE_BUSY_TIMEOUT_MS` and `SCHEDULER_MAX_WORKERS`.

### Carry-forward (informational, not changed)
- WAL `PRAGMA` result row is not fetched — harmless in pysqlite (test confirms WAL activates).
- File-backed SQLite keeps pysqlite's default `check_same_thread=True`; safe because FSA's
  `QueuePool` + scoped session give each worker thread its own connection (created on that
  thread). Differs intentionally from netdiag's single-shared-connection model.
