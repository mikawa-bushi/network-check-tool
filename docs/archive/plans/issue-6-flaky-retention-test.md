# Plan: issue-6 flaky test_downsample_collapses_old_keeps_recent

## Goal (verbatim from issue #6)

`tests/test_retention.py::test_downsample_collapses_old_keeps_recent` is time-dependent and
fails ~1.6% of the time depending on the wall-clock minute. After this is fixed, the test
should pass deterministically regardless of when it runs.

## Root cause (confirmed)

The test inserts 60 "old" samples at `old + i` for `i in 0..59` where
`old = now - 10*86400`, then asserts they collapse into exactly **one** hourly rollup
(`ts // 3600` bucket). Because `864000` is a multiple of `3600`,
`old mod 3600 == now mod 3600`, so when `now mod 3600 ∈ [3541, 3599]` (the last ~59s of an
epoch-hour) the 60-second window straddles a bucket boundary → 2 rollups → assertion fails.
`netdiag/storage.py` bucketing is correct; this is purely test-data construction.

## Approach (test-only)

1. Extract the insert pattern into a module-level helper `_seed_old_and_recent(st, now)` so the
   real test and a new deterministic guard test share one construction.
2. Snap the old window to the start of an hour bucket:
   `old = (int(now - 10 * 86400) // 3600) * 3600 + 1` — guarantees `old .. old+59` sit in one
   `ts // 3600` bucket for any `now`. (Mirrors the existing snapping in
   `test_rollup_running_average_is_correct`.)
3. Add a guard test `test_downsample_collapse_deterministic_at_hour_boundary` that monkeypatches
   `time.time` to the worst case (`*3600 + 3599`) and asserts `rollups == 1`. This pins the
   regression: reverting the snap makes this test fail deterministically.

No change to `netdiag/storage.py` or any other production code.

## Alternatives considered / rejected
- **Mock time only in the existing test**: doesn't add a permanent regression guard for the
  worst-case boundary; a future edit could silently reintroduce the flake.
- **Change `downsample` bucketing**: out of scope and incorrect — production behaviour is right.
- **`freezegun`**: avoids a new dependency; `monkeypatch.setattr(time, "time", ...)` is enough
  since `storage.py` imports the `time` module and calls `time.time()`.

## Out of scope
- `test_downsample_is_idempotent` uses a similar `now - 10*86400` window but its assertions
  (idempotency + counts unchanged) are insensitive to bucket splitting, so it is left as-is.
- Other retention tests already snap to bucket boundaries or insert a single row.

## Test strategy (acceptance scenarios → tests)
- "Passes deterministically regardless of when it runs" + "the 60 old samples are placed inside
  a single hour bucket by construction" → the snapped `_seed_old_and_recent` + the time-pinned
  guard test (`rollups == 1` at `now mod 3600 == 3599`).
- "No production code in storage.py changed" → diff is limited to `tests/test_retention.py`
  (+ plan/version/changelog).

## Version / changelog
- Bump `netdiag.__version__` 0.1.1 → 0.1.2 (patch; test-reliability fix, no runtime change).
- Add `netdiag/CHANGELOG.md` with the v0.1.2 entry. (Root `pyproject.toml` is the
  network_check_tool app version and is intentionally not touched here.)
