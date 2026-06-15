# Findings: issue-6-flaky-retention-test

## Round 1

### Code Reviewer
- Blocking: 0
- Should Fix: 1
- Informational: 7
- Key Issues:
  - `test_downsample_is_idempotent` still uses the unsnapped `old = now - 10*86400`.
    Confirmed safe (it only asserts idempotency + stable counts, insensitive to bucket
    splitting), but visually identical to the pre-fix pattern — a readability trap for
    future copy-paste into a test that *does* assert a rollup count.
- Judgment: Math and scope independently verified (snap correct for all 3600 `now mod 3600`;
  failure window 59/3600 ≈ 1.64%; guard pins worst case; storage.py untouched). The one
  Should-Fix is a real maintainability nit.

### Doc Parrot
- Divergences Found: 0
- Details:
  - `_seed_old_and_recent()` (tests/test_retention.py:16) — **OK**. Prose accurately
    describes seeding 60 old (one bucket) + 4 recent samples derived from `now`, and the
    "one ts//3600 bucket regardless of now" guarantee is realised by the snap. Commit
    side effect implied by "Seed".
  - The two changed `test_*` callables are skipped (test functions).
- Judgment: No docstring divergence.

### Actions taken (Round 1)
- Added an explanatory comment on `test_downsample_is_idempotent`'s unsnapped `old`
  warning against copying the pattern into rollup-count-sensitive tests (issue #6).
