# Findings: issue-16-sql-aggregation-index

## Round 1

### Code Reviewer
- Blocking: 1
- Should Fix: 2
- Informational: 8
- Key Issues:
  - **Blocking**: `func.avg/min/max` return `Decimal` on PostgreSQL (supported via
    `DATABASE_URL`), which Flask's JSON encoder can't serialize → 500 on Postgres with any
    successful rows. Faithfully reproduced the doc's P1 snippet, but the bug is real.
  - Should Fix: no characterization test for the `packet_loss=False, response_time=None` edge
    row; the `is_(False)` strictness vs old `not r.packet_loss` was undocumented.
- Judgment: Excellent review — caught a genuine cross-backend serialization bug the behaviour
  tests (SQLite-only) would never surface. All items addressed.

### Doc Parrot
- Divergences Found: 0
- Details:
  - No changed callable carries a docstring needing verification: `get_stats`'s one-line
    docstring ("Get overall statistics") is unchanged and still accurate (same overall stats,
    now SQL-computed). The new test functions have no docstrings.
- Judgment: Nothing to flag.

### Actions taken (Round 1)
- Cast `avg/min/max` to `float()` (Decimal→float on Postgres; no-op on SQLite) — fixes the
  Blocking serialization bug.
- Added `test_stats_excludes_no_response_non_loss_row` (packet_loss=False + response_time=None
  counts toward total/failed, excluded from avg/min/max).
- Documented the `is_(False)` strictness in a code comment (column is NOT NULL, so NULL
  packet_loss should never occur).

### Carry-forward (informational, not changed)
- The `or 0` on `COUNT(*)` results is redundant (COUNT never returns NULL) but harmless/defensive.
- No-migration caveat (create_all won't add the index to pre-existing DBs) is documented in the
  plan, CHANGELOG, and a code comment; an idempotent runtime CREATE INDEX is a possible follow-up.
