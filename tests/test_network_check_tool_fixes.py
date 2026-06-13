"""Regression tests for issue #4 (network_check_tool low-cost fix bundle).

Covers five long-standing defects called out in docs/improvements.md:

* U2 -- the ``status`` header dropped the space before "for <host>".
* U6 -- a 0.0 ms response was mislabelled "timeout" (0.0 is falsy).
* M2 -- ``PING_TIMEOUT`` was never wired into PingService (CLI ping + scheduler).
* M3 -- ``views/dashboard.py`` imported ``desc``/``func`` but never used them.
* E4 -- the dashboard API 500'd on ``?hours=abc`` and ignored out-of-range values.
"""

from datetime import datetime

import pytest
from click.testing import CliRunner
from flask import Flask

from network_check_tool import cli as cli_module
from network_check_tool.cli import cli
from network_check_tool.config.config import Config
from network_check_tool.models import db, PingResult
from network_check_tool.services import SchedulerService
from network_check_tool.views.dashboard import dashboard_bp


def _make_app(db_path):
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


def _make_web_app(db_path):
    app = _make_app(db_path)
    app.register_blueprint(dashboard_bp)
    with app.app_context():
        db.create_all()
    return app


def _seed(app, **kwargs):
    defaults = dict(
        target_host="google.com",
        timestamp=datetime(2026, 6, 13, 12, 0, 0),
        response_time=12.34,
        packet_loss=False,
        error_message=None,
    )
    defaults.update(kwargs)
    with app.app_context():
        db.session.add(PingResult(**defaults))
        db.session.commit()


# --------------------------------------------------------------------------- U2


def test_status_header_has_space_before_host(tmp_path, monkeypatch):
    """U2: header must read 'Recent ping results for <host>:' (space preserved)."""
    app = _make_web_app(tmp_path / "u2.db")
    _seed(app, target_host="google.com")
    monkeypatch.setattr(cli_module, "create_app", lambda *a, **k: app)

    result = CliRunner().invoke(cli, ["status", "--host", "google.com"])

    assert result.exit_code == 0, result.output
    assert "Recent ping results for google.com:" in result.output
    assert "resultsfor" not in result.output


# --------------------------------------------------------------------------- U6


def test_zero_response_time_is_not_timeout(tmp_path, monkeypatch):
    """U6: a measured 0.0 ms RTT with no packet loss shows '0.00ms', not 'timeout'."""
    app = _make_web_app(tmp_path / "u6.db")
    _seed(app, target_host="fast.local", response_time=0.0, packet_loss=False)
    monkeypatch.setattr(cli_module, "create_app", lambda *a, **k: app)

    result = CliRunner().invoke(cli, ["status", "--host", "fast.local"])

    assert result.exit_code == 0, result.output
    assert "0.00ms" in result.output
    assert "timeout" not in result.output


def test_packet_loss_still_shows_timeout(tmp_path, monkeypatch):
    """U6: a genuine no-response (packet_loss) result still reads 'timeout'."""
    app = _make_web_app(tmp_path / "u6b.db")
    _seed(app, target_host="down.local", response_time=None, packet_loss=True)
    monkeypatch.setattr(cli_module, "create_app", lambda *a, **k: app)

    result = CliRunner().invoke(cli, ["status", "--host", "down.local"])

    assert result.exit_code == 0, result.output
    assert "timeout" in result.output


# --------------------------------------------------------------------------- M2


def test_scheduler_service_forwards_timeout():
    """M2: SchedulerService(timeout=...) must reach its PingService."""
    scheduler = SchedulerService(timeout=2.5)
    try:
        assert scheduler.ping_service.timeout == 2.5
    finally:
        scheduler.shutdown()


def test_scheduler_service_default_timeout_unchanged():
    """M2: with no timeout argument the PingService keeps its 4.0s default."""
    scheduler = SchedulerService()
    try:
        assert scheduler.ping_service.timeout == 4.0
    finally:
        scheduler.shutdown()


def test_cli_ping_uses_config_timeout(tmp_path, monkeypatch):
    """M2: the `ping` command constructs PingService with Config.PING_TIMEOUT."""
    app = _make_web_app(tmp_path / "m2.db")
    monkeypatch.setattr(cli_module, "create_app", lambda *a, **k: app)
    monkeypatch.setattr(Config, "PING_TIMEOUT", 7.5)

    captured = {}

    class FakePingService:
        def __init__(self, timeout=4.0):
            captured["timeout"] = timeout

    import network_check_tool.services as services_module

    monkeypatch.setattr(services_module, "PingService", FakePingService)

    # --count 0 means no actual network pings are issued.
    result = CliRunner().invoke(cli, ["ping", "example.com", "--count", "0"])

    assert result.exit_code == 0, result.output
    assert captured.get("timeout") == 7.5


# --------------------------------------------------------------------------- M3


def test_dashboard_has_no_unused_sqlalchemy_imports():
    """M3: the unused `desc`/`func` imports were removed from the dashboard view."""
    import network_check_tool.views.dashboard as dash

    assert not hasattr(dash, "desc")
    assert not hasattr(dash, "func")


# --------------------------------------------------------------------------- E4


def test_invalid_hours_returns_400(tmp_path):
    """E4: a non-integer ?hours= yields a 400 JSON error, not a 500."""
    app = _make_web_app(tmp_path / "e4a.db")
    client = app.test_client()

    resp = client.get("/api/stats?hours=abc")

    assert resp.status_code == 400
    assert resp.is_json
    assert "error" in resp.get_json()


def test_out_of_range_hours_is_clamped(tmp_path):
    """E4: a negative/out-of-range ?hours= is clamped to a sane range (no 500)."""
    app = _make_web_app(tmp_path / "e4b.db")
    client = app.test_client()

    resp = client.get("/api/stats?hours=-5")

    assert resp.status_code == 200
    assert resp.is_json


@pytest.mark.parametrize("route", ["/api/ping_data", "/api/ping_chart", "/api/stats"])
def test_invalid_hours_rejected_on_all_routes(tmp_path, route):
    """E4: every hours-consuming endpoint rejects bad input the same way."""
    app = _make_web_app(tmp_path / "e4c.db")
    client = app.test_client()

    resp = client.get(f"{route}?host=google.com&hours=abc")

    assert resp.status_code == 400, route
