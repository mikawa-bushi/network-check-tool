# Findings: issue-12-storage-thread-safety

## Round 1

### Code Reviewer
- Blocking: 0
- Should Fix: 3
- Informational: 11
- Key Issues:
  - `VACUUM` in `retention()` runs while the RLock is held → can stall background writers up
    to `busy_timeout` (5 s) during periodic maintenance.
  - `executescript()` implicitly commits; the trailing `commit()` is redundant.
  - Monitor test's explicit `st.close()` is asymmetric with the Watcher test (which relies on
    `watcher.stop()` to close) — a future Monitor.stop() that closes storage would double-close.
- Judgment: Strong review. Confirmed every `self.conn` method is synchronized, RLock
  re-entrancy is correct for both call chains, WAL/`:memory:`/existing-DB behaviour is safe,
  and the semver/plan are sound.

### Doc Parrot
- Divergences Found: 0
- Details:
  - `_synchronized()` (netdiag/storage.py) — **OK**. Prose ("serialize a Storage method on
    self._lock, held for the method's duration") matches `with self._lock: return method(...)`.
  - `record_result` / `downsample` kept pre-existing, still-accurate docstrings (only a
    decorator was added above them; the locking is transparent to their contract).
  - Inner `wrapper` and test callables have no docstrings → skipped.
- Judgment: No divergence.

### Actions taken (Round 1)
- Added the Monitor-test asymmetry comment (`Monitor.stop()` does not own storage).
- Documented `executescript()`'s implicit commit (trailing commit is harmless belt-and-suspenders).
- **Kept VACUUM under the lock by design** and documented why: the lock protects the shared
  `conn` *object*, not just transactional consistency — releasing it during VACUUM (as the
  reviewer suggested) would let a worker thread use the same connection concurrently, re-opening
  exactly the bug this issue fixes. `retention()` is hourly maintenance, so the bounded hold is
  acceptable. (Disagreed-with-reason on this Should-Fix.)

### Carry-forward (informational, not changed)
- `busy_timeout=5000` is a conservative default (no measured data).
- Monitor/Watcher background tests are timing-based (3 s deadline, 60× margin over the 50 ms
  tick) — low but non-zero CI flakiness risk; an Event-based signal could remove it later.
