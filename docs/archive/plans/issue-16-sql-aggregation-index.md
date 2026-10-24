# Plan: issue-16 dashboard stats SQL aggregation + composite index (P1/P6)

## Goal (verbatim from issue #16)

ダッシュボードの統計 API をデータ量に対してスケールさせる（P1 + P6）。統計集計を Python の
全行ループから SQL 集計に置き換え、`PingResult` に実クエリと一致する複合インデックス
`(target_host, timestamp)` を追加して、行数が増えてもレイテンシが線形悪化しないようにする。

## Findings (exact current semantics — must preserve)

`views/dashboard.py get_stats`: loads `query.all()` then computes in Python:
- `successful = [r for r in results if not r.packet_loss and r.response_time is not None]`
- `total_pings = len(results)`; `successful_pings = len(successful)`;
  `failed_pings = total - successful`; `packet_loss_rate = (total-successful)/total*100`;
  `avg/min/max` over the successful rows' `response_time`, else `0`.
- Empty range → a zeros dict.

`models/ping_result.py`: `target_host` and `timestamp` each have a **separate single-column**
index (`index=True`); no composite. Real query is
`target_host == ? AND timestamp >= ? ORDER BY timestamp`.

## Approach

### P1 — SQL aggregation (views/dashboard.py get_stats)
Replace the `query.all()` + Python loops with DB-side aggregation, preserving the exact
definition of "successful" (`packet_loss is False AND response_time IS NOT NULL`):
```python
from sqlalchemy import func
base = db.session.query(PingResult).filter(PingResult.timestamp >= since)
if host:
    base = base.filter(PingResult.target_host == host)
total = base.with_entities(func.count()).scalar() or 0
if total == 0:
    return jsonify({ ...zeros... })
ok = base.filter(PingResult.packet_loss.is_(False), PingResult.response_time.isnot(None))
successful = ok.with_entities(func.count()).scalar() or 0
avg_v, min_v, max_v = ok.with_entities(
    func.avg(PingResult.response_time),
    func.min(PingResult.response_time),
    func.max(PingResult.response_time),
).one()
```
`failed = total - successful`; `packet_loss_rate = (total-successful)/total*100`;
avg/min/max default to `0` when `successful == 0` (SQL returns NULL → coerce to 0). JSON shape
and values stay identical.

### P6 — composite index (models/ping_result.py)
Add `__table_args__ = (db.Index("ix_ping_results_host_ts", "target_host", "timestamp"),)`.
Drop the now-redundant single-column index on `target_host` (the composite's leading column
serves `target_host`-only lookups and `get_hosts`' DISTINCT scan). Keep `timestamp index=True`
(serves the host=None time-range stats query). This matches the doc's "separate single columns
→ one composite" intent.

## Alternatives considered / rejected
- **Keep the redundant `target_host` single index**: harmless but a half-fix (extra write/disk
  for zero benefit once the composite exists). Dropped it.
- **One combined aggregate query with conditional SUM(CASE ...)**: fewer round-trips, but two
  small `COUNT`s + one `avg/min/max` row is clearer and the index makes them cheap. Kept simple.
- **Alembic migration**: app uses `db.create_all()`, no migration framework. Out of scope; the
  index lands for new DBs via `create_all` (noted below).

## Out of scope
- P5 (`get_ping_data`/`get_ping_chart` row cap / downsampling) — separate Issue.
- P2 (probe parallelization), P3 (network_check_tool DB WAL / scheduler thread count).
- Migrating existing DBs: `create_all` won't add the index to a pre-existing table. Documented;
  an idempotent runtime `CREATE INDEX IF NOT EXISTS` is left for a follow-up if needed.

## Test strategy (acceptance scenarios → tests)
New `tests/test_dashboard_stats.py` (manual Flask app + db + dashboard_bp, mirroring existing
tests):
- **P1 correctness (characterization; refactor preserves values)**: seed known rows
  (successes with rtts, a packet-loss row, a no-response row, a second host); assert
  `/api/stats?host=X` and `/api/stats` (all hosts) return exact totals/rates/avg/min/max;
  assert empty DB → zeros dict.
- **P6 (red→green)**: assert `PingResult.__table__.indexes` contains a composite over
  `(target_host, timestamp)`.

## Version / changelog
- Bump root `pyproject.toml` 0.2.0 → 0.2.1 (network_check_tool; perf improvement, backward
  compatible). Add a v0.2.1 CHANGELOG entry.
