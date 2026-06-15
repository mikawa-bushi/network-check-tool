"""Tests for issue #16 (P1/P6): dashboard stats SQL aggregation + composite index.

P1: ``/api/stats`` must compute aggregates in SQL while returning the exact same
values as the previous Python-side computation (characterization).
P6: ``PingResult`` must carry a composite ``(target_host, timestamp)`` index.
"""

from flask import Flask

from network_check_tool.models import PingResult, db
from network_check_tool.utils import utcnow
from network_check_tool.views.dashboard import dashboard_bp


def _make_app(tmp_path):
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{tmp_path / 'stats.db'}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    app.register_blueprint(dashboard_bp)
    with app.app_context():
        db.create_all()
    return app


def _seed(app, rows):
    # Recent timestamp: /api/stats filters to the last 24h, so a fixed past date
    # would fall outside the window and make these aggregates read as empty.
    now = utcnow()
    with app.app_context():
        for host, rt, loss in rows:
            db.session.add(
                PingResult(
                    target_host=host,
                    timestamp=now,
                    response_time=rt,
                    packet_loss=loss,
                    error_message=None,
                )
            )
        db.session.commit()


# ------------------------------------------------------------------------ P1


def test_stats_for_host_matches_known_aggregates(tmp_path):
    app = _make_app(tmp_path)
    # host "a": three successes (10/20/30 ms) + one packet loss (no response).
    _seed(
        app,
        [
            ("a", 10.0, False),
            ("a", 20.0, False),
            ("a", 30.0, False),
            ("a", None, True),
            ("b", 99.0, False),  # other host, must be excluded by host filter
        ],
    )
    data = app.test_client().get("/api/stats?host=a").get_json()

    assert data["total_pings"] == 4
    assert data["successful_pings"] == 3
    assert data["failed_pings"] == 1
    assert data["packet_loss_rate"] == 25.0
    assert data["avg_response_time"] == 20.0
    assert data["min_response_time"] == 10.0
    assert data["max_response_time"] == 30.0


def test_stats_excludes_no_response_non_loss_row(tmp_path):
    # Edge case: packet_loss=False but response_time is NULL. It must count toward
    # total and toward "failed", but never toward avg/min/max (same as the old code).
    app = _make_app(tmp_path)
    _seed(
        app,
        [
            ("a", 10.0, False),
            ("a", 20.0, False),
            ("a", None, False),  # not a loss, but no measured response time
        ],
    )
    data = app.test_client().get("/api/stats?host=a").get_json()

    assert data["total_pings"] == 3
    assert data["successful_pings"] == 2
    assert data["failed_pings"] == 1
    assert data["avg_response_time"] == 15.0
    assert data["min_response_time"] == 10.0
    assert data["max_response_time"] == 20.0


def test_stats_all_hosts(tmp_path):
    app = _make_app(tmp_path)
    _seed(
        app,
        [
            ("a", 10.0, False),
            ("a", None, True),
            ("b", 30.0, False),
        ],
    )
    data = app.test_client().get("/api/stats").get_json()

    assert data["total_pings"] == 3
    assert data["successful_pings"] == 2
    assert data["failed_pings"] == 1
    assert round(data["packet_loss_rate"], 4) == round((1 / 3) * 100, 4)
    assert data["avg_response_time"] == 20.0
    assert data["min_response_time"] == 10.0
    assert data["max_response_time"] == 30.0


def test_stats_empty_returns_zeros(tmp_path):
    app = _make_app(tmp_path)
    data = app.test_client().get("/api/stats").get_json()

    assert data == {
        "total_pings": 0,
        "successful_pings": 0,
        "failed_pings": 0,
        "packet_loss_rate": 0,
        "avg_response_time": 0,
        "min_response_time": 0,
        "max_response_time": 0,
    }


# ------------------------------------------------------------------------ P6


def test_pingresult_has_composite_host_timestamp_index():
    index_col_tuples = [
        tuple(c.name for c in idx.columns) for idx in PingResult.__table__.indexes
    ]
    assert (
        "target_host",
        "timestamp",
    ) in index_col_tuples, f"expected composite index; found {index_col_tuples}"
