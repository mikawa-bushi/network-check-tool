"""Tests for baseline learning, change-point and flapping detection, and storage."""

import time

from netdiag.diagnosis import (
    compute_baseline,
    classify,
    detect_changepoint,
    detect_flapping,
)
from netdiag.core import CheckResult, Status
from netdiag.storage import Storage


def test_baseline_median_and_anomaly():
    values = [10, 11, 9, 10, 12, 10, 11, 9, 10, 11]
    base = compute_baseline(values)
    assert base is not None
    assert 9 <= base.median <= 12
    assert classify(10, base) == "normal"
    assert classify(500, base) == "anomalous"


def test_baseline_needs_minimum_samples():
    assert compute_baseline([1, 2]) is None


def test_changepoint_detects_shift():
    # 20 samples ~10ms then 20 samples ~200ms.
    t0 = 1_000_000.0
    series = [(t0 + i, 10.0) for i in range(20)] + \
             [(t0 + 20 + i, 200.0) for i in range(20)]
    cp = detect_changepoint(series)
    assert cp is not None
    assert cp.worsened
    assert cp.index == 20  # shift is exactly at the boundary


def test_changepoint_none_when_stable():
    series = [(float(i), 10.0 + (i % 2)) for i in range(40)]
    assert detect_changepoint(series) is None


def test_flapping_detected():
    # Alternating good/bad loss values.
    series = [(float(i), 100.0 if i % 2 else 0.0) for i in range(20)]
    rate = detect_flapping(series)
    assert rate is not None and rate > 0.5


def test_storage_roundtrip(tmp_path):
    db = tmp_path / "h.db"
    with Storage(db) as st:
        r = CheckResult(name="external", status=Status.OK, summary="ok",
                        metrics={"rtt_ms": 12.3, "loss_pct": 0.0})
        st.record_result(r, target="1.1.1.1")
        st.record_result(
            CheckResult(name="external", status=Status.OK, summary="ok",
                        metrics={"rtt_ms": 14.0, "loss_pct": 0.0}),
            target="1.1.1.1",
        )
        vals = st.values("external", "rtt_ms", target="1.1.1.1")
        assert vals == [12.3, 14.0]
        assert "external" in st.probes_seen()
