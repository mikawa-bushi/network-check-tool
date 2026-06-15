# Findings: issue-22-pingservice-netdiag-core

## Round 1

### Code Reviewer
- Blocking: 0
- Should Fix: 2
- Informational: 6
- Key Issues:
  - **Should Fix**: the success condition (`rtt is None or loss >= 100.0`) would silently treat a
    reply-with-partial-loss as a success if `count` were ever raised above 1, and nothing
    documented/tested that. Added a code comment explaining the `count=1` invariant (loss is only
    0 or 100) and a characterization test pinning the partial-loss-as-success behaviour.
  - **Should Fix**: timeout semantics — `self.timeout` is forwarded as netdiag's per-reply `-W`,
    but netdiag's `run()` caps the whole subprocess at `count*timeout+5s` (≈9s worst case for the
    4.0s default), longer than the old per-ping ping3 deadline. Documented this in a comment at
    the call site (the forwarded value is unchanged; only the worst-case block time differs).
  - Informational (verified): units handled correctly (netdiag returns ms, so the old `*1000` is
    correctly dropped — pinned by `test_success_maps_rtt_in_ms`); `have("ping")` guard and the
    monkeypatch target are correct; no consumer reads the raw dict (all go through
    `ping_and_save` → `PingResult`), so the contract is preserved.
- Judgment: Accurate, useful review. No correctness bugs; both Should-Fix items were latent
  documentation/edge-case gaps and are now covered.

### Doc Parrot
- Divergences Found: 0
- Details:
  - The collection script auto-detected no callables: `ping_host`'s `def` line is byte-identical
    to the original (only its body changed and a docstring was *added*), so a `--unified=0` diff
    doesn't surface the `def` line. Reviewed `ping_host` manually since it is a changed callable
    with a new docstring.
  - `ping_host` — **OK**: the docstring's described return dict (keys `host`, `timestamp`,
    `response_time` in ms or `None` on failure, `packet_loss` True on no reply, `error_message`)
    matches the implementation exactly; units (ms) and failure semantics verified by the tests.
- Judgment: Nothing to flag.

### Actions taken (Round 1)
- `ping_service.py`: comment documenting the `count*timeout+5` subprocess cap; comment documenting
  the `count=1` (0/100 loss) success invariant.
- `tests`: added `test_reply_with_partial_loss_counts_as_success`.

### Carry-forward (informational, not changed)
- Later M1 phases will unify time-series storage and the scheduler with `netdiag`; this phase was
  ping measurement only.
