# Findings: issue-20-parallel-probes

## Round 1

### Code Reviewer
- Blocking: 0
- Should Fix: 2
- Informational: 6
- Key Issues:
  - **Should Fix**: `test_independent_probes_run_concurrently` relied on absolute wall-clock
    bounds (`parallel < 2*sleep`), fragile on loaded CI. Replaced with a structural test
    (`test_dependency_tiers_group_independent_probes`, no timing) plus a generous ratio-based
    timing check (`parallel < serial * 0.6`, sleep raised to 0.2 s).
  - **Should Fix**: the verbose skip-as-consequence reasoning string wasn't compared serial-vs-
    parallel. Added `test_failed_dependency_skip_reasoning_identical_serial_vs_parallel`.
  - **Informational (verified)**: the only probe that writes to the shared `Context` during
    `check()` is `gateway.py`'s `ctx.config.setdefault("_gateway", gw)`. Confirmed `_gateway` is
    never read anywhere (neighbor recomputes via `default_gateway()`), so it's an atomic
    (GIL), dead write — tier concurrency is safe. Documented the read-only-ctx assumption in a
    comment at the parallel-execution site.
- Judgment: Strong, accurate review. No correctness bugs; the thread-safety question it raised
  was real and resolved by verification + a documenting comment. Both Should-Fix items addressed.

### Doc Parrot
- Divergences Found: 1
- Details:
  - `_dependency_tiers` (`engine.py:85`) — **Fix docstring (gap)**: the prose described tiers by
    "selected dependencies" but never said the dependency data is read from each probe's
    *registered* `depends_on` (not from the arguments). Verified by running it with unregistered
    names → `[['x','y']]` (all level 0). Patched the docstring to state this and the
    unregistered-name behaviour.
  - `_evaluate_probe` (`engine.py:137`) — **OK**: prose matches behaviour. Verified it never
    writes `ctx.results` and returns `(None, None)` for an unknown probe name.
- Judgment: Caught a real, if minor, omission about where dependency info comes from; fixed.

### Actions taken (Round 1)
- Tests: added structural tier test; relaxed the timing test to a ratio; added verbose
  skip-reasoning parity test.
- `engine.py`: documented the read-only-ctx thread-safety assumption at the parallel branch;
  patched `_dependency_tiers` docstring to note dependencies come from the registry.

### Carry-forward (informational, not changed)
- `gateway.py`'s `_gateway` setdefault is a dead write; removing it is out of scope for this
  issue (gateway.py wasn't otherwise touched) but is a reasonable future cleanup.
