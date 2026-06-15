# Plan: issue #18 — SQLite WAL + busy_timeout, right-size scheduler pool (P3)

## Goal (verbatim from issue #18)

> The `network_check_tool` dashboard's SQLite database tolerates concurrent monitoring writes
> without spurious `database is locked` errors, and the monitoring scheduler no longer reserves
> an oversized worker pool. (Improvements doc item **P3**; the `netdiag` side of P3 was already
> handled under M4.)

## Approach

Two independent, small changes in `network_check_tool`.

### 1. SQLite WAL + busy_timeout (conditional on dialect)

`flask_sqlalchemy` opens the dashboard DB with no journal-mode/timeout tuning, so a dashboard
reader and the monitoring writer can collide into an immediate `database is locked`. Mirror what
`netdiag.storage` already does (WAL + `busy_timeout=5000`), but only when the engine is SQLite —
`DATABASE_URL` may point at PostgreSQL, where those PRAGMAs are invalid.

- Extract a small helper `_register_sqlite_pragmas(engine, busy_timeout_ms) -> bool` in
  `app.py`. It returns `False` (no-op) for non-SQLite engines; for SQLite it attaches a
  SQLAlchemy `connect` event listener that runs `PRAGMA journal_mode=WAL` and
  `PRAGMA busy_timeout=<ms>` on every new DBAPI connection, and returns `True`.
- In `create_app`, after `db.init_app(app)` and **before** `db.create_all()` (the first
  connection use), call the helper with `db.engine` and `app.config["SQLITE_BUSY_TIMEOUT_MS"]`.
- New config knob `SQLITE_BUSY_TIMEOUT_MS` (env `SQLITE_BUSY_TIMEOUT_MS`, default `5000`,
  matching netdiag).

Extracting the helper keeps it unit-testable against a throwaway file-backed engine without
having to reconstruct the whole app with a custom DB path (config is import-time evaluated).

### 2. Right-size the scheduler worker pool

`SchedulerService` hardcodes `ThreadPoolExecutor(20)` — far more than the handful of monitored
hosts need. Make it a small, configurable default:

- Module constant `DEFAULT_SCHEDULER_MAX_WORKERS = 4` in `scheduler_service.py`.
- `SchedulerService.__init__(self, app=None, timeout=None, max_workers=None)`; when `max_workers`
  is `None`, fall back to the module default. Record it as `self.max_workers` and build
  `ThreadPoolExecutor(self.max_workers)` from it.
- New config knob `SCHEDULER_MAX_WORKERS` (env, default `4`); `cli.py start_monitoring` passes
  `max_workers=Config.SCHEDULER_MAX_WORKERS`, consistent with how `timeout` is injected (M2).

## Alternatives considered / rejected

- **Global `@event.listens_for(Engine, "connect")`** (the common Flask-SQLAlchemy recipe):
  rejected — it applies to every engine in the process. App-scoped registration on `db.engine`
  is tidier and keeps test apps isolated.
- **Auto-sizing the pool to `len(DEFAULT_HOSTS)`**: rejected — hosts are passed to
  `start-monitoring` at the CLI edge, not necessarily the config default. A small configurable
  constant with headroom is simpler and predictable; operators with many hosts raise the env var.

## Out of scope

- `netdiag` (already WAL + `busy_timeout` + `check_same_thread=False` via M4).
- Probe parallelization (that's P2, issue TBD).
- Any schema / migration changes.

## Testing (acceptance scenarios → tests)

- **WAL on SQLite**: register pragmas on a real file-backed engine, open a connection, assert
  `PRAGMA journal_mode` → `wal` and `PRAGMA busy_timeout` → configured value.
- **No PRAGMA on non-SQLite + clean start**: helper returns `False` for a stub engine whose
  `dialect.name == "postgresql"` (no driver needed, guard returns before touching the engine).
- **End-to-end via create_app**: build the app (default SQLite), assert the live `db.engine`
  connection reports WAL.
- **Configurable pool**: `SchedulerService(max_workers=7).max_workers == 7`; default is the
  module constant and is meaningfully smaller than the old 20.
- **Config knobs exist**: `Config.SQLITE_BUSY_TIMEOUT_MS` and `Config.SCHEDULER_MAX_WORKERS`
  are ints with the documented small defaults.
