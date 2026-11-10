# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

### Removed
- `network-checker init`. Every command already creates the database.

## [0.5.0] - 2026-11-09

The first release on PyPI since 0.1.1, so it also brings everything listed
under 0.2.0 to 0.4.0 below.

### Fixed
- **Monitoring no longer drops devices when some are unreachable.** Each device
  used to be its own APScheduler job on a 4-thread pool; an unreachable device
  holds a thread for the whole ping timeout, and jobs that could not start
  within a second of their slot were skipped. With 20 unreachable and 5 healthy
  devices at a 5 s interval, the healthy ones recorded nothing for 33 s and 16
  of the unreachable ones never got a sample (so no outage alert either).
  Devices sharing an interval are now pinged together, once per interval, with
  netdiag's concurrent `ping_many`: the same test records every sample of every
  device. Each sweep writes its rows in one transaction; with `--diagnose`, the
  hosts that went down together are diagnosed in one netdiag run (this PC's
  network is checked once, not once per device).

### Changed
- APScheduler is no longer a dependency (the monitor reuses netdiag's periodic
  loop). The `SCHEDULER_MAX_WORKERS` setting is gone.
- The first measurement runs as soon as monitoring starts (it used to wait one
  interval), so the line board fills in within seconds.
- `GET /api/jobs` lists one entry per device: `host`, `interval`,
  `next_run_time` (the `job_id` field is gone).

### Removed
- The `DEFAULT_PING_INTERVAL` and `DEFAULT_HOSTS` environment variables. Nothing
  read them, so setting them had no effect. Use `--interval` and a hosts or
  device list instead.

## [0.4.0] - 2026-10-28

Field readiness: verified on every OS in CI, precise on Windows, a single exe for
PCs without Python, hours-long drop hunting, and a line board in the dashboard.
netdiag 0.3.0 (see `netdiag/CHANGELOG.md`).

### Added
- **`netdiag soak`**: continuous measurement for hours — outage timeline, outages
  correlated across devices (all / some / one → this PC's side / a shared switch /
  that device), NIC errors (and link drops on Linux), live start/end lines,
  text/JSON/CSV/HTML heatmap reports.
- **`netdiag collect`**: zips the raw output of the OS commands netdiag parses plus
  its own interpretation, with IPs/MACs/host names/SSIDs/domains masked while
  keeping subnet structure and MAC vendors.
- **Windows `netdiag.exe`** (PyInstaller, no Python needed): double-click checks this
  PC, dropping a device list on it checks the line; the HTML report is saved and
  opened. Built and smoke-tested by `.github/workflows/windows-exe.yml`, attached
  to `v*` releases with a Japanese/English README and a sample device list.
- **Dashboard line board** (`network-checker`): every device as a tile by name and
  group with OK/Warning/Down/Unknown (icon + word), loss, RTT, last reply, why it
  is amber, and the netdiag-diagnosed cause of an outage; recent alerts with the
  outage length and next steps. Japanese UI (English by browser or switch).
  `network-checker run-server --devices FILE` monitors a device list and serves the
  board in one command; `start-monitoring --config` labels the board too.
  New API: `GET /api/overview`, `GET /api/alerts`; `POST /api/hosts` takes
  `name`/`group`/`note`. Alerts are stored in the DB (`alert_events`), device names
  in `monitored_hosts` (both created automatically).
- CI: the test suite plus live smoke runs (`check`, `ping`, `soak`, `collect`) on
  Ubuntu, Windows and macOS; the `collect` zips are kept as artifacts.

### Changed
- Precise pings on Windows without admin rights (Windows ICMP API) — `check`,
  `soak` and `network-checker` no longer fall back to `ping.exe`'s 1 request/s.
- Monitor alert summaries follow the display language (Japanese by default).
- `network-checker clear` also clears stored alerts (device names are kept).
- Dependencies: flask 3.1.3, flask-cors 6.0, werkzeug 3.1.9, pytest 9, black 26,
  virtualenv/filelock/Pygments updated for published advisories. (filelock stays at
  4.1.0 on Python 3.10 dev environments: the fixed release needs Python 3.11+.)

### Fixed
- CI ran only on branches without a `/` in their name (`'*'` → `'**'`).
- Output redirected to a file or pipe on a Japanese/English Windows console no
  longer crashes on characters the code page cannot encode.

## [0.3.0] - 2026-10-21

Refocused on production engineers: check this PC **and every device on the line**
quickly, precisely and comprehensively. netdiag 0.2.0 (see `netdiag/CHANGELOG.md`).

### Added
- `netdiag check` takes devices (`192.168.3.39:502`, `PLC1=192.168.3.39:modbus`) and
  device lists (`-f`: Excel CSV in Shift_JIS/UTF-8/UTF-16, YAML, JSON, TOML; Japanese
  or English headers; protocol names for ports). Each device gets a route → reach →
  ports diagnosis with a root cause and numbered next steps.
- `netdiag scan` discovers devices on a subnet (ICMP + ARP only) and can save a
  device-list CSV.
- Reports in text, JSON (stable result codes), self-contained HTML and CSV for Excel;
  Japanese by default, English with `--lang en` / `NETDIAG_LANG=en`.
- `network-checker start-monitoring --config` accepts the same device lists; with
  `--diagnose`, a degraded host is diagnosed specifically (this PC's link, subnet,
  ARP presence, ping statistics) instead of a generic internet sweep.
- Windows support for netdiag's host checks (PowerShell / `route print` / `arp -a`,
  language-independent), `python -m netdiag`.
- `examples/devices.csv`, `examples/line1.yaml`; a working `docker/` setup for the
  dashboard and monitor.

### Changed
- **`netdiag check` no longer assumes internet access**: external reachability,
  DNS and IPv6 checks run only with `--internet` (or `--external-ip`/`--dns-name`),
  and a missing default gateway is reported as normal on a closed network.
- Human-facing output is Japanese by default.
- Ping measurements use an unprivileged ICMP socket where permitted (microsecond
  timing, no `ping` binary needed) and report loss with a 95 % upper bound and
  median/p95/max/jitter; `network-checker`'s samples use the same core.

### Removed
- The unused `pandas` dependency (and its transitive packages).
- Material copied from an unrelated project: `docs/qiita_usage_guide.md`, the old
  Monitor App Docker files and contributing guide (rewritten for this project).
- Internal issue-plan notes moved from the package directories to `docs/archive/`
  (they were shipped inside the wheel).

## [0.2.5] - 2026-06-15

### Changed (network_check_tool, issue #37)

- **M7**: replaced the deprecated `datetime.utcnow()` (deprecated in Python 3.12) with a shared
  `network_check_tool.utils.utcnow()` helper. It returns the same naive-UTC instant via
  `datetime.now(timezone.utc).replace(tzinfo=None)`, so stored `PingResult.timestamp` values and the
  dashboard's time-range comparisons are byte-for-byte unchanged — this is a forward-compatibility
  cleanup, not a timezone-storage change.

## [0.2.4] - 2026-06-15

### Fixed (network_check_tool, issue #33)

- **U1**: the dashboard no longer loads Plotly from `cdn.plot.ly/plotly-latest` — it serves the
  bundle shipped with the installed `plotly` package from a local route (`/vendor/plotly.min.js`).
  The dashboard now renders charts with no outbound network access, and the Plotly version is
  pinned to the dependency instead of a moving CDN tag.

## [0.2.3] - 2026-06-15

### Changed (network_check_tool, issue #22)

- **M1 (phase 1)**: `PingService` no longer carries its own ICMP implementation — it now measures
  via `netdiag`'s shared, stdlib-only `ping` core (one ping per sample, returning milliseconds) and
  guards on the `ping` command being available. The returned result dict, `save_ping_result` /
  `ping_and_save`, the stored `PingResult` fields, and the `PING_TIMEOUT` wiring are all unchanged.
- The `ping3` dependency is removed (`pyproject.toml` + `poetry.lock`); ping measurement no longer
  needs raw-socket privileges. Unifying storage and the scheduler with `netdiag` are deferred to
  later M1 phases.

## [0.2.2] - 2026-06-15

### Performance (network_check_tool, issue #18)

- The dashboard's SQLite database is now opened in WAL journal mode with a
  `busy_timeout` (default 5000 ms, `SQLITE_BUSY_TIMEOUT_MS`), so a dashboard reader and the
  monitoring writer no longer collide into an immediate "database is locked". The PRAGMAs are
  applied only when the backend is SQLite; a non-SQLite `DATABASE_URL` (e.g. PostgreSQL) is
  untouched (P3).
- The monitoring scheduler's worker pool is now a small, configurable default
  (`SCHEDULER_MAX_WORKERS`, default 4) instead of a hardcoded 20 threads (P3).

## [0.2.1] - 2026-06-15

### Performance (network_check_tool, issue #16)

- `/api/stats` now computes its aggregates (count / packet-loss rate / avg / min / max response
  time) in SQL instead of loading every row in the time range into Python. Response shape and
  values are unchanged; latency no longer grows linearly with stored ping volume (P1).
- `PingResult` gains a composite `(target_host, timestamp)` index matching the dashboard's
  host + time-range + ordered query; the redundant single-column `target_host` index is dropped
  (the composite's leading column covers it). New databases get the index via `create_all`;
  pre-existing databases would need it created manually (no migration framework) (P6).

## [0.2.0] - 2026-06-15

### Security (network_check_tool, issue #10)

- **BREAKING**: `run-server` now binds to `127.0.0.1` (localhost) by default instead of
  `0.0.0.0`. To expose the dashboard/API on the network, pass `--host 0.0.0.0` explicitly;
  a warning is printed when binding to a non-loopback address.
- `--debug` (the Werkzeug interactive debugger, an arbitrary-code-execution vector) is now
  refused on non-loopback hosts. Debug only on `--host 127.0.0.1`.
- CORS is no longer all-permissive. Cross-origin access to `/api/*` is granted only when
  `CORS_ORIGINS` (comma-separated allowed origins) is configured; the default sends no
  permissive CORS headers. Same-origin dashboard use is unaffected.
- README: added a security note covering the localhost default, reverse-proxy/auth guidance,
  and the debug/CORS behaviour.

## [0.1.2] - 2026-06-13

### Fixed (network_check_tool, issue #4)

- **U2**: `network-checker status --host <host>` no longer renders
  `Recent ping resultsfor <host>:` — the missing space before `for` is restored.
- **U6**: a measured `0.0 ms` response time with no packet loss is now shown as
  `0.00ms` instead of being mislabelled `timeout`. The display is keyed on
  `packet_loss` / `response_time is None`, consistent with the Status column.
- **M2**: `PING_TIMEOUT` is now honoured. The CLI `ping` command and the
  monitoring scheduler both use `Config.PING_TIMEOUT` instead of the hardcoded
  4.0s default. `SchedulerService` accepts a `timeout` argument.
- **M3**: removed unused `desc` / `func` imports from `views/dashboard.py`.
- **E4**: dashboard API endpoints (`/api/ping_data`, `/api/ping_chart`,
  `/api/stats`) validate the `?hours=` parameter — a non-integer value returns a
  `400` JSON error instead of a `500`, and out-of-range values are clamped to
  `[1, 168]`.

## [0.1.1]

- Fixed `start-monitoring` crash ("Schedulers cannot be serialized").
