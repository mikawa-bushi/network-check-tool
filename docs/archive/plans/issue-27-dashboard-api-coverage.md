# Plan: issue #27 — dashboard API endpoint coverage (M10 phase 2)

## Goal (verbatim from issue #27)

> The dashboard's HTTP endpoints have unit tests covering their normal (happy-path) behaviour and
> their error branches, not just the `?hours=` validation already tested.

## Approach

New `tests/test_dashboard_api.py`. Build the app through the real `create_app()` factory with a
tmp SQLite DB (monkeypatch `Config.SQLALCHEMY_DATABASE_URI`, as in
`tests/test_sqlite_wal_scheduler_pool.py`) so templates render and config is realistic; seed
`PingResult` rows via `db.session` inside an app context (as in `tests/test_dashboard_stats.py`).
Use Flask's test client.

Endpoints / cases (all currently untested beyond `?hours=` validation):

- `GET /` → `200` (renders `dashboard.html`).
- `GET /api/hosts` → `200`, JSON list equal to the distinct seeded hosts.
- `GET /api/ping_data?host=h` → `200`; assert `timestamps`/`response_times`/`packet_losses`
  lengths and `total_pings`/`successful_pings`/`packet_loss_rate` reflect the seed (incl. a
  packet-loss row giving `response_times` a `None` and a `1` in `packet_losses`).
  - no `host` → `400`.
  - host with no rows → `404`.
- `GET /api/ping_chart?host=h` → `200`, body parses as a Plotly figure (has `data` + `layout`).
  - no `host` → `400`.
  - host with no rows → `404`.

These are characterization tests of existing, correct behaviour — no red phase and no runtime
change. They feed a throwaway DB (no network) and run in the CI pytest job.

## Out of scope

- Any change to view logic; the two CLIs and `netdiag/report.py` (later M10 phases); `/api/stats`
  (covered by #16) and `?hours=` validation (already covered).

## Versioning

Test-only, no runtime change → no version bump / CHANGELOG entry (justified in findings), as in #25.
