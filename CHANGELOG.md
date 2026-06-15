# Changelog

All notable changes to this project are documented in this file.

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
