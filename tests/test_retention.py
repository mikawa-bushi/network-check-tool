"""Tests for storage retention: downsampling old raw data into hourly rollups."""

import time

from netdiag.storage import Storage


def _insert(st, ts, value, probe="external", target="1.1.1.1", metric="rtt_ms"):
    st.conn.execute(
        "INSERT INTO samples (ts,probe,target,status,metric,value,summary) "
        "VALUES (?,?,?,?,?,?,?)",
        (ts, probe, target, "ok", metric, value, "ok"),
    )


def _seed_old_and_recent(st, now):
    """Seed 60 'old' samples inside a single hour bucket plus 4 recent samples.

    The 60 old samples are placed one second apart starting at ``old``. ``old`` is
    derived from ``now`` so callers can pin the clock; the construction must keep all
    60 inside one ``ts // 3600`` bucket regardless of ``now``.
    """
    old = now - 10 * 86400
    for i in range(60):
        _insert(st, old + i, 10.0 + (i % 5))  # old, within one hour bucket
    for i in range(4):
        _insert(st, now - i, 12.0)  # recent
    st.conn.commit()


def test_downsample_collapses_old_keeps_recent(tmp_path):
    st = Storage(tmp_path / "h.db")
    now = time.time()
    _seed_old_and_recent(st, now)

    assert st.counts() == {"samples": 64, "rollups": 0}
    collapsed = st.downsample(older_than_days=7)
    assert collapsed == 60
    c = st.counts()
    assert c["samples"] == 4  # recent raw kept
    assert c["rollups"] == 1  # 60 old rows -> one hourly bucket
    # recent raw still readable for baselining
    assert st.values("external", "rtt_ms", target="1.1.1.1") == [12.0, 12.0, 12.0, 12.0]
    st.close()


def test_downsample_collapse_deterministic_at_hour_boundary(tmp_path, monkeypatch):
    # Pin the clock to the last second of an epoch hour -- the window that used to make
    # the (now - 10d, +0..59s) construction straddle two ts//3600 buckets and flake
    # (~1.6% of runs). With the bucket-snapped seed the 60 old rows must always
    # collapse into exactly one rollup; reverting the snap makes this fail every run.
    bad_now = (int(time.time()) // 3600) * 3600 + 3599
    monkeypatch.setattr(time, "time", lambda: bad_now)

    st = Storage(tmp_path / "boundary.db")
    _seed_old_and_recent(st, time.time())
    st.downsample(older_than_days=7)
    assert st.counts()["rollups"] == 1
    st.close()


def test_downsample_is_idempotent(tmp_path):
    st = Storage(tmp_path / "h.db")
    old = time.time() - 10 * 86400
    for i in range(30):
        _insert(st, old + i, 5.0)
    st.conn.commit()
    st.downsample(older_than_days=7)
    before = st.counts()
    # Running again with no new old data must not change anything.
    assert st.downsample(older_than_days=7) == 0
    assert st.counts() == before
    st.close()


def test_rollup_running_average_is_correct(tmp_path):
    st = Storage(tmp_path / "h.db")
    bucket_start = (int(time.time() - 10 * 86400) // 3600) * 3600 + 1
    for v in (10.0, 20.0, 30.0):  # same hour bucket
        _insert(st, bucket_start, v)
    st.conn.commit()
    st.downsample(older_than_days=7)
    row = st.conn.execute("SELECT count, avg, min, max FROM rollups").fetchone()
    assert row["count"] == 3
    assert abs(row["avg"] - 20.0) < 1e-9
    assert row["min"] == 10.0 and row["max"] == 30.0
    st.close()


def test_prune_rollups_drops_ancient(tmp_path):
    st = Storage(tmp_path / "h.db")
    st.conn.execute(
        "INSERT INTO rollups (bucket_ts,probe,target,metric,count,avg,min,max) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (time.time() - 200 * 86400, "external", "1.1.1.1", "rtt_ms", 5, 10, 8, 12),
    )
    st.conn.commit()
    assert st.prune_rollups(older_than_days=90) == 1
    assert st.counts()["rollups"] == 0
    st.close()
