# Plan: issue #37 — replace deprecated datetime.utcnow() (M7)

## Goal (verbatim from issue #37)

> `network_check_tool` no longer calls the deprecated `datetime.utcnow()` (deprecated since Python
> 3.12), so the package is forward-compatible and free of the associated deprecation warnings.

## Sites (network_check_tool only; netdiag has none)

- `models/ping_result.py:20` — `default=datetime.utcnow` (callable ref).
- `services/ping_service.py:23` — `datetime.utcnow()`.
- `views/dashboard.py:98,146,221` — `datetime.utcnow() - timedelta(hours=hours)`.
- Tests added this session: `test_dashboard_api.py`, `test_cli_commands.py`, `test_dashboard_stats.py`.

## Approach

Add `network_check_tool/utils.py` with:

```python
def utcnow() -> datetime:
    """Current UTC time as a naive datetime (drop-in for the deprecated
    datetime.utcnow()). Naive on purpose: the PingResult.timestamp column and the
    dashboard's time-range comparisons are naive UTC, so this preserves behaviour
    exactly while avoiding the 3.12 deprecation."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
```

`datetime.now(timezone.utc).replace(tzinfo=None)` yields the **same naive UTC instant** as
`datetime.utcnow()` — so storage format, `timestamp >= since` comparisons, and `isoformat()` output
are all unchanged. Then replace every site:

- model: `default=utcnow` (callable).
- ping_service / dashboard: `utcnow()`.
- the three test sites: import and use `utcnow()` too (so no deprecation debt remains).

## Alternatives considered / rejected

- **`datetime.now(timezone.utc)` (timezone-aware), as the doc literally suggests**: rejected for
  this ★ cleanup — storing aware datetimes in the naive `db.DateTime` column risks changing the
  stored string format and the dashboard's comparisons (and SQLite doesn't store tz anyway). A
  proper tz-aware redesign (column `timezone=True` + aware everywhere + "Z" in the API) is a
  separate, larger change, explicitly out of scope here.

## Out of scope

- `netdiag`; changing the timestamp column type or the API serialisation.

## Testing (acceptance scenarios → tests) — `tests/test_utcnow.py`

- `utcnow()` returns a `datetime` with `tzinfo is None` (naive) within a second or two of "now".
- Regression guard: every `*.py` under `network_check_tool/` contains no literal `datetime.utcnow`
  (catches both `datetime.utcnow()` and the `default=datetime.utcnow` callable ref). This is what
  fails red before the sweep and stays green after.

## Versioning

Cleanup with no behaviour change → bump root `pyproject.toml` (0.2.4 → 0.2.5) + CHANGELOG.
