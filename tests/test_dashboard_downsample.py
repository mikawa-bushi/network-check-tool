"""Tests for dashboard downsampling + tz-aware timestamps (P1/U1, 2026-06-17).

``_downsample`` must pass rows through unchanged at/below the cap (so the existing
ping_data/ping_chart contract is preserved) and bound the point count above it
while keeping packet-loss events visible. Timestamps handed to Plotly must be
tz-aware UTC so the browser never reads them as the viewer's local time.
"""

from datetime import datetime, timedelta, timezone

from flask import Flask

from network_check_tool.models import PingResult, db
from network_check_tool.utils import utcnow
from network_check_tool.views import dashboard as dash
from network_check_tool.views.dashboard import dashboard_bp


class _Row:
    """Stand-in for a PingResult row (avoids a DB for the pure-function tests)."""

    def __init__(self, ts, rt, loss):
        self.timestamp = ts
        self.response_time = rt
        self.packet_loss = loss


def _make_rows(n, loss_every=0):
    base = datetime(2026, 1, 1)  # naive UTC, as stored
    rows = []
    for i in range(n):
        is_loss = bool(loss_every) and (i % loss_every == 0)
        rows.append(
            _Row(base + timedelta(seconds=i), None if is_loss else float(i), is_loss)
        )
    return rows


# ------------------------------------------------------------------- _as_utc


def test_as_utc_tags_naive_as_utc():
    assert dash._as_utc(datetime(2026, 1, 1, 12)).tzinfo == timezone.utc


def test_as_utc_leaves_aware_untouched():
    aware = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    assert dash._as_utc(aware) is aware


# ---------------------------------------------------------------- _downsample


def test_downsample_passthrough_below_cap():
    pts = dash._downsample(_make_rows(5), max_points=10)
    assert [p.response_time for p in pts] == [0.0, 1.0, 2.0, 3.0, 4.0]
    assert all(p.timestamp.tzinfo == timezone.utc for p in pts)


def test_downsample_bounds_points_above_cap():
    rows = _make_rows(1000)
    pts = dash._downsample(rows, max_points=100)
    # Each bucket emits at most a success point + a loss point.
    assert len(pts) <= 200
    assert len(pts) < len(rows)
    assert all(p.timestamp.tzinfo == timezone.utc for p in pts)


def test_downsample_keeps_loss_markers_when_thinning():
    # 1000 rows with a loss every 50th; even thinned hard, loss buckets must
    # still surface a loss marker so outages stay visible.
    pts = dash._downsample(_make_rows(1000, loss_every=50), max_points=10)
    assert any(p.packet_loss for p in pts)


# --------------------------------------------------------------- HTTP surface


def _make_app(tmp_path):
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{tmp_path / 'ds.db'}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    app.register_blueprint(dashboard_bp)
    with app.app_context():
        db.create_all()
    return app


def test_ping_data_timestamps_are_utc_aware(tmp_path):
    app = _make_app(tmp_path)
    now = utcnow()
    with app.app_context():
        db.session.add(
            PingResult(
                target_host="a",
                timestamp=now,
                response_time=10.0,
                packet_loss=False,
                error_message=None,
            )
        )
        db.session.commit()
    data = app.test_client().get("/api/ping_data?host=a").get_json()
    assert data["downsampled"] is False
    # tz-aware UTC isoformat ends with the explicit +00:00 offset.
    assert data["timestamps"][0].endswith("+00:00")
