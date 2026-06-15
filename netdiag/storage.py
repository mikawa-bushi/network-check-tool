"""Time-series persistence with the standard-library ``sqlite3`` only.

No ORM, no SQLAlchemy: a single dependency-free file the rest of the tool reads
to answer 'since when?' and to learn baselines. Storing every probe metric (not
just ping RTT) means change-point detection works for DNS latency, TTFB, etc.
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

DEFAULT_DB = Path.home() / ".netdiag" / "history.db"


def _synchronized(method):
    """Serialize a Storage method on ``self._lock``.

    The single sqlite connection is shared across threads (Monitor/Watcher run
    their loops in a background thread), so every method that touches it holds
    the re-entrant lock for the duration of its work.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL    NOT NULL,
    probe     TEXT    NOT NULL,
    target    TEXT,
    status    TEXT    NOT NULL,
    metric    TEXT    NOT NULL,
    value     REAL,
    summary   TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_probe_metric_ts
    ON samples (probe, metric, ts);
CREATE INDEX IF NOT EXISTS idx_samples_target_ts
    ON samples (target, ts);

-- Downsampled aggregates of old raw samples. One row per (hour, probe, target,
-- metric); raw rows are deleted once rolled up, so disk stays bounded over months
-- while long-range trends remain queryable.
CREATE TABLE IF NOT EXISTS rollups (
    bucket_ts REAL    NOT NULL,
    probe     TEXT    NOT NULL,
    target    TEXT,
    metric    TEXT    NOT NULL,
    count     INTEGER NOT NULL,
    avg       REAL,
    min       REAL,
    max       REAL,
    PRIMARY KEY (bucket_ts, probe, target, metric)
);
"""


class Storage:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else DEFAULT_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False lets the background loops (Monitor/Watcher
        # start(background=True)) use this connection. A single sqlite connection
        # object is not safe for *concurrent* use, so a re-entrant lock serializes
        # every access below. RLock (not Lock) because some public methods call
        # others (record_diagnosis -> record_result, retention -> downsample).
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            # WAL improves reader/writer concurrency; busy_timeout avoids spurious
            # "database is locked" under contention. (WAL is a no-op on :memory:.)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
            # executescript() implicitly commits before running; the trailing
            # commit() is belt-and-suspenders and harmless.
            self.conn.executescript(_SCHEMA)
            self.conn.commit()

    # -- writes ---------------------------------------------------------------
    @_synchronized
    def record_result(self, result, target: Optional[str] = None) -> None:
        """Persist every numeric metric of a CheckResult as one row each.

        Probes with no metrics still get a status row (value NULL) so we can chart
        availability over time.
        """
        ts = getattr(result, "timestamp", time.time())
        rows = []
        if result.metrics:
            for metric, value in result.metrics.items():
                rows.append(
                    (
                        ts,
                        result.name,
                        target,
                        result.status.value,
                        metric,
                        float(value),
                        result.summary,
                    )
                )
        else:
            rows.append(
                (
                    ts,
                    result.name,
                    target,
                    result.status.value,
                    "_status",
                    None,
                    result.summary,
                )
            )
        self.conn.executemany(
            "INSERT INTO samples (ts, probe, target, status, metric, value, summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()

    @_synchronized
    def record_diagnosis(self, diagnosis, target: Optional[str] = None) -> None:
        for r in diagnosis.results:
            self.record_result(r, target=target)

    # -- reads ----------------------------------------------------------------
    @_synchronized
    def series(
        self,
        probe: str,
        metric: str,
        target: Optional[str] = None,
        since: Optional[float] = None,
    ) -> List[Tuple[float, float]]:
        q = (
            "SELECT ts, value FROM samples WHERE probe=? AND metric=? "
            "AND value IS NOT NULL"
        )
        params: List = [probe, metric]
        if target is not None:
            q += " AND target=?"
            params.append(target)
        if since is not None:
            q += " AND ts>=?"
            params.append(since)
        q += " ORDER BY ts"
        return [(row["ts"], row["value"]) for row in self.conn.execute(q, params)]

    def values(
        self,
        probe: str,
        metric: str,
        target: Optional[str] = None,
        since: Optional[float] = None,
    ) -> List[float]:
        return [v for _, v in self.series(probe, metric, target, since)]

    @_synchronized
    def probes_seen(self) -> List[str]:
        return [
            r["probe"]
            for r in self.conn.execute(
                "SELECT DISTINCT probe FROM samples ORDER BY probe"
            )
        ]

    @_synchronized
    def prune(self, older_than_days: float = 30) -> int:
        """Delete raw samples older than the cutoff. Returns rows removed."""
        cutoff = time.time() - older_than_days * 86400
        cur = self.conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount

    @_synchronized
    def downsample(self, older_than_days: float = 7, bucket_seconds: int = 3600) -> int:
        """Roll raw samples older than the cutoff into per-bucket aggregates.

        Groups old rows by (time bucket, probe, target, metric), writes count/
        avg/min/max into ``rollups``, then deletes the raw rows. Returns the
        number of raw rows collapsed. Idempotent: re-running merges new old rows
        into existing buckets via running-average accumulation.
        """
        cutoff = time.time() - older_than_days * 86400
        rows = self.conn.execute(
            "SELECT CAST(ts / ? AS INT) * ? AS bucket, probe, target, metric, "
            "COUNT(value) AS n, AVG(value) AS a, MIN(value) AS mn, MAX(value) AS mx "
            "FROM samples WHERE ts < ? AND value IS NOT NULL "
            "GROUP BY bucket, probe, target, metric",
            (bucket_seconds, bucket_seconds, cutoff),
        ).fetchall()

        collapsed = 0
        for r in rows:
            existing = self.conn.execute(
                "SELECT count, avg, min, max FROM rollups WHERE bucket_ts=? AND "
                "probe=? AND (target IS ?) AND metric=?",
                (r["bucket"], r["probe"], r["target"], r["metric"]),
            ).fetchone()
            if existing and existing["count"]:
                tot = existing["count"] + r["n"]
                avg = (existing["avg"] * existing["count"] + r["a"] * r["n"]) / tot
                mn = min(existing["min"], r["mn"])
                mx = max(existing["max"], r["mx"])
                self.conn.execute(
                    "UPDATE rollups SET count=?, avg=?, min=?, max=? WHERE "
                    "bucket_ts=? AND probe=? AND (target IS ?) AND metric=?",
                    (
                        tot,
                        avg,
                        mn,
                        mx,
                        r["bucket"],
                        r["probe"],
                        r["target"],
                        r["metric"],
                    ),
                )
            else:
                self.conn.execute(
                    "INSERT OR REPLACE INTO rollups "
                    "(bucket_ts, probe, target, metric, count, avg, min, max) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        r["bucket"],
                        r["probe"],
                        r["target"],
                        r["metric"],
                        r["n"],
                        r["a"],
                        r["mn"],
                        r["mx"],
                    ),
                )
            collapsed += r["n"]

        self.conn.execute(
            "DELETE FROM samples WHERE ts < ? AND value IS NOT NULL", (cutoff,)
        )
        self.conn.commit()
        return collapsed

    @_synchronized
    def prune_rollups(self, older_than_days: float = 90) -> int:
        cutoff = time.time() - older_than_days * 86400
        cur = self.conn.execute("DELETE FROM rollups WHERE bucket_ts < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount

    @_synchronized
    def retention(
        self, raw_days: float = 7, rollup_days: float = 90, bucket_seconds: int = 3600
    ) -> dict:
        """One-shot maintenance: downsample old raw, drop ancient rollups.

        Keeps full-resolution data for ``raw_days`` and hourly aggregates up to
        ``rollup_days``. Returns a small summary for logging.
        """
        collapsed = self.downsample(
            older_than_days=raw_days, bucket_seconds=bucket_seconds
        )
        dropped = self.prune_rollups(older_than_days=rollup_days)
        # Reclaim file space after large deletes. VACUUM runs under the lock on
        # purpose: it shares the single connection object, so a concurrent write
        # from a Monitor/Watcher worker thread would corrupt it. retention() is a
        # periodic maintenance call (hourly by default), so the bounded hold
        # (up to busy_timeout) is acceptable; releasing the lock here would not be.
        try:
            self.conn.execute("VACUUM")
        except sqlite3.OperationalError:
            pass
        return {"raw_collapsed": collapsed, "rollups_dropped": dropped}

    @_synchronized
    def counts(self) -> dict:
        s = self.conn.execute("SELECT COUNT(*) AS c FROM samples").fetchone()["c"]
        r = self.conn.execute("SELECT COUNT(*) AS c FROM rollups").fetchone()["c"]
        return {"samples": s, "rollups": r}

    @_synchronized
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
