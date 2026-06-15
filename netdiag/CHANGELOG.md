# Changelog — netdiag

All notable changes to the `netdiag` package are documented in this file.

## [0.1.3] - 2026-06-15

### Changed (issue #8)

- **Dead-code removal (M6)**: dropped the unused `user_line = "" if system else ""` local in
  `cli.py`'s `_unit_text`. No change to the generated systemd unit. Added characterization
  tests for `_unit_text` (user/system variants) plus a guard against reintroducing the dead
  local.

## [0.1.2] - 2026-06-14

### Fixed (issue #6)

- **Test reliability**: `tests/test_retention.py::test_downsample_collapses_old_keeps_recent`
  was time-dependent and failed ~1.6% of the time — when `now` fell in the last ~59 seconds of
  an epoch-hour, the 60-second seed window straddled an `ts // 3600` bucket boundary and the
  60 old samples split across two rollups. The seed window is now snapped to the start of an
  hour bucket so it always collapses into one rollup, and a clock-pinned guard test locks the
  worst case. Test-only change; `netdiag/storage.py` behaviour is unchanged.

## [0.1.1]

- Earlier releases.
