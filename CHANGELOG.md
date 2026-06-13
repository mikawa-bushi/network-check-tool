# Changelog

All notable changes to this project are documented in this file.

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
