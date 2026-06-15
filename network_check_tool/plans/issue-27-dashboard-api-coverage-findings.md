# Findings: issue-27-dashboard-api-coverage

## Round 1

### Code Reviewer
- Blocking: 1
- Should Fix: 3
- Informational: 6
- Key Issues:
  - **Blocking**: `_seed` used a hardcoded `datetime(2026, 6, 15, ...)`, but `/api/ping_data` and
    `/api/ping_chart` filter to the last 24h — after 2026-06-16 12:00 UTC the rows would expire
    and the happy-path tests would 404, breaking CI permanently. Fixed by seeding `utcnow()`-
    relative timestamps. **Also fixed the same latent time-bomb in `tests/test_dashboard_stats.py`
    (authored in #16)** — `/api/stats` filters the same window, so now that pytest gates CI (#26)
    it would have broken tomorrow too.
  - **Should Fix**: tied timestamps made `ORDER BY timestamp` non-deterministic for the
    `[10.0, None, 30.0]` assertion → now seeds strictly increasing timestamps.
  - **Should Fix**: the chart happy-path only checked `data`/`layout` keys (present even in an
    empty figure) → now asserts `len(fig["data"]) >= 1` and the host title.
  - **Should Fix**: `create_app("development")` sets `SQLALCHEMY_ECHO=True` (noisy CI) → fixture
    monkeypatches `DevelopmentConfig.SQLALCHEMY_ECHO=False` (verified 0 SQL lines in output).
- Judgment: Excellent review — caught a real, imminent CI-breaking time-bomb (and its twin in a
  sibling file) plus genuine test-strength gaps. All items addressed.

### Doc Parrot
- Divergences Found: 0
- Details: the changed callables are test functions and the `app`/`_seed` fixtures (no docstrings);
  no view logic changed. Nothing to review.
- Judgment: N/A.

### Actions taken (Round 1)
- `test_dashboard_api.py`: `utcnow()`-relative, strictly-increasing seed timestamps; stronger chart
  assertion (trace count + title); silence `SQLALCHEMY_ECHO`.
- `test_dashboard_stats.py`: `utcnow()`-relative seed timestamp (fix the same window time-bomb).

### Carry-forward (informational, not changed)
- `_seed` is now duplicated (with the same shape) across the two dashboard test files; a shared
  `tests/conftest.py` fixture would dedupe them. Deferred — both are self-correcting now (no
  hardcoded date), so the duplication is low-risk.
- No version bump: test-only change.
