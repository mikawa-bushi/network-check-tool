"""Tests for issue #27 (M10 phase 2): dashboard HTTP endpoint coverage.

Covers the happy-path and error branches of the dashboard blueprint's endpoints
that were untested beyond ``?hours=`` validation: the index page, ``/api/hosts``,
``/api/ping_data``, and ``/api/ping_chart``. (``/api/stats`` is covered by #16.)

The app is built through the real factory with a throwaway SQLite database, so
templates render and no real network is touched.
"""

from datetime import timedelta

import pytest

from network_check_tool.app import create_app
from network_check_tool.config.config import Config, DevelopmentConfig
from network_check_tool.models import PingResult, db
from network_check_tool.utils import utcnow


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(
        Config, "SQLALCHEMY_DATABASE_URI", f"sqlite:///{tmp_path / 'dash.db'}"
    )
    # Keep test output quiet: DevelopmentConfig echoes every SQL statement.
    monkeypatch.setattr(DevelopmentConfig, "SQLALCHEMY_ECHO", False)
    application = create_app("development")
    yield application


def _seed(app, rows):
    # Seed *recent* rows with strictly increasing timestamps: the endpoints filter
    # to the last 24h (so a fixed past date would silently expire and 404), and the
    # ORDER BY timestamp needs distinct values to be deterministic.
    base = utcnow() - timedelta(minutes=5)
    with app.app_context():
        for i, (host, rt, loss) in enumerate(rows):
            db.session.add(
                PingResult(
                    target_host=host,
                    timestamp=base + timedelta(seconds=i),
                    response_time=rt,
                    packet_loss=loss,
                    error_message=None,
                )
            )
        db.session.commit()


# ------------------------------------------------------------------------ index


def test_index_returns_200(app):
    resp = app.test_client().get("/")
    assert resp.status_code == 200


# ------------------------------------------------------------------------ hosts


def test_hosts_lists_distinct_hosts(app):
    _seed(app, [("a", 10.0, False), ("a", 12.0, False), ("b", 20.0, False)])
    data = app.test_client().get("/api/hosts").get_json()
    assert sorted(data) == ["a", "b"]


def test_hosts_empty(app):
    assert app.test_client().get("/api/hosts").get_json() == []


# --------------------------------------------------------------------- ping_data


def test_ping_data_happy_path(app):
    _seed(app, [("a", 10.0, False), ("a", None, True), ("a", 30.0, False)])
    data = app.test_client().get("/api/ping_data?host=a").get_json()

    assert data["host"] == "a"
    assert len(data["timestamps"]) == 3
    # response_times carries None where a packet was lost; losses series is 0/1.
    assert data["response_times"] == [10.0, None, 30.0]
    assert data["packet_losses"] == [0, 1, 0]
    assert data["total_pings"] == 3
    assert data["successful_pings"] == 2
    assert round(data["packet_loss_rate"], 4) == round((1 / 3) * 100, 4)


def test_ping_data_requires_host(app):
    assert app.test_client().get("/api/ping_data").status_code == 400


def test_ping_data_no_data_returns_404(app):
    assert app.test_client().get("/api/ping_data?host=ghost").status_code == 404


# -------------------------------------------------------------------- ping_chart


def test_ping_chart_happy_path(app):
    _seed(app, [("a", 10.0, False), ("a", None, True), ("a", 30.0, False)])
    resp = app.test_client().get("/api/ping_chart?host=a")
    assert resp.status_code == 200
    fig = resp.get_json()
    assert "data" in fig and "layout" in fig
    # Not just shape: a real figure with at least one trace and the host title.
    assert len(fig["data"]) >= 1
    assert "Ping Results for a" in str(fig["layout"]["title"])


def test_ping_chart_requires_host(app):
    assert app.test_client().get("/api/ping_chart").status_code == 400


def test_ping_chart_no_data_returns_404(app):
    assert app.test_client().get("/api/ping_chart?host=ghost").status_code == 404
