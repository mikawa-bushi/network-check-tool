# Plan: issue-12 Storage thread safety / start(background=True) (M4)

## Goal (verbatim from issue #12)

`netdiag` の公開 API の既定（`Monitor(...).start()` / `Watcher(...).start()` は `background=True`）が
スレッド安全でなく、利用者が素直に呼ぶと sqlite のクロススレッド `ProgrammingError` になる問題を
解消する。バックグラウンドスレッドからの記録・読み取りが安全に動作するようにする。

## Findings (confirmed)

- `storage.py:55` `self.conn = sqlite3.connect(str(self.path))` — default `check_same_thread=True`,
  one connection created on the main thread, no WAL.
- `scheduler.py:Monitor._loop` and `watch.py:Watcher._loop` run in a `threading.Thread`
  (`start(background=True)` default) and call `self.storage.record_diagnosis(...)` from that
  thread → `sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in
  that same thread`. Both `_loop`s swallow exceptions (`except Exception: pass`), so the bug
  manifests as **silently recording nothing**, not a crash. CLI uses `background=False`.

## Approach (fix entirely in Storage)

1. Open the connection with `check_same_thread=False` so it can be used from the worker thread.
2. Add a `threading.RLock` and guard **every** DB-touching method with `with self._lock:`.
   RLock (re-entrant) so `record_diagnosis`→`record_result` and `retention`→`downsample`/
   `prune_rollups` don't self-deadlock. This serializes all access to the single connection,
   which is required for correctness once it is shared across threads.
3. Enable `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` at init for better
   read/write concurrency and to avoid spurious "database is locked" under contention.
   (WAL on `:memory:` is a no-op — harmless.)

No change to public signatures (`Storage(path)`, `Monitor.start(background=True)`,
`Watcher.start(background=True)`), and no change to CLI behaviour.

## Alternatives considered / rejected
- **Per-thread connections** (thread-local): more code, and write serialization is still needed
  to avoid "database is locked"; the single-connection + RLock approach is simpler and
  sufficient at this scale. Rejected.
- **Queue/worker thread for writes**: heavier; unnecessary for the modest write rate here.
- **`sqlite3.connect(..., check_same_thread=False)` without a lock**: unsafe — concurrent use
  of one connection object can interleave/cursor-corrupt. Rejected.

## Out of scope
- `network_check_tool` DB (separate package, SQLAlchemy).
- P3 broader WAL/perf tuning beyond what correctness needs; M5/M7.

## Test strategy (acceptance scenarios → tests)
New `tests/test_storage_thread_safety.py`:
- **cross-thread write**: write via `record_result` from a separate thread; no exception, and
  the main thread reads back 1 sample. (Red pre-fix: ProgrammingError.)
- **concurrent writers**: N threads each writing M rows complete without error; total count
  is exact (serialization holds).
- **Monitor background**: `Monitor(interval=…, storage=st)` with a stub engine, `start()`
  (background), poll until `counts()["samples"] > 0`, `stop()`. (Red pre-fix: stays 0.)
- **Watcher background**: same with `Watcher(...)`, a stub engine, null sink, `escalate=False`.

Stub engine returns a canned diagnosis (results=[fake CheckResult], primary=None, healthy=True)
to avoid real network and stay deterministic.

## Version / changelog
- Bump `netdiag.__version__` 0.1.3 → 0.1.4 (patch; thread-safety bug fix).
- Add a v0.1.4 entry to `netdiag/CHANGELOG.md`.
