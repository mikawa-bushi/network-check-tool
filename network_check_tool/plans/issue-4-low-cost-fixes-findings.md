# Findings: issue-4-low-cost-fixes

## Round 1

### Code Reviewer
- Blocking: 0
- Should Fix: 3
- Informational: 8
- Key Issues:
  - M2-b test patches `network_check_tool.services.PingService`; works today because
    `cli.py` uses a deferred import, but brittle if the import is later hoisted.
  - `test_out_of_range_hours_is_clamped` only exercised `/api/stats`; the other two
    hours endpoints had no clamping-path assertion.
  - New scheduler tests construct a live `BackgroundScheduler`, which re-triggers the
    pre-existing `logging.basicConfig` side effect (E2, out of scope).
- Judgment: Caught two real test-quality gaps worth fixing; the third is genuinely
  out of scope (E2). No correctness bugs in production code.

### Doc Parrot
- Divergences Found: 0
- Details:
  - `_parse_hours()` (network_check_tool/views/dashboard.py:16) — **OK**. Prose
    accurately describes the `(hours, None)` / `(None, (json, 400))` return contract
    and the `[HOURS_MIN, HOURS_MAX]` clamping. Request-context dependency and the
    missing-param default are implied by "?hours= query parameter".
  - `SchedulerService.__init__` — skipped (dunder, no docstring).
  - All other changed callables are test functions/helpers — out of scope.
- Judgment: No docstring divergences. The one documented helper is faithful to its code.

### Actions taken (Round 1)
- Added a clarifying comment in the M2-b test explaining why the package-attribute
  patch target is correct for the deferred import.
- Added `test_out_of_range_hours_handled_gracefully` (parametrized over all three
  endpoints × {-5, 0, 100000}) to cover the clamping path everywhere.
- Left the E2 `basicConfig` side effect untouched (explicitly out of scope for #4).
