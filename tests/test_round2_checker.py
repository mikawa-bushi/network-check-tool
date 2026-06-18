"""Round-2 review features (network-checker side).

F8 -- ``network-checker status``/``ping`` gain a ``--json`` machine-readable mode.
"""

import json

from click.testing import CliRunner
from flask import Flask

from network_check_tool import cli as cli_module
from network_check_tool.cli import cli
from network_check_tool.models import PingResult, db
from network_check_tool.services.ping_service import PingService
from network_check_tool.utils import utcnow
from network_check_tool.views.dashboard import dashboard_bp


def _make_web_app(db_path):
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    app.register_blueprint(dashboard_bp)
    with app.app_context():
        db.create_all()
    return app


def _fake_ping(self, host):
    return {
        "host": host,
        "timestamp": utcnow(),
        "response_time": 5.0,
        "packet_loss": False,
        "error_message": None,
    }


# --------------------------------------------------------------------------- F8


def test_status_json_outputs_array(tmp_path, monkeypatch):
    app = _make_web_app(tmp_path / "s.db")
    monkeypatch.setattr(cli_module, "create_app", lambda *a, **k: app)
    with app.app_context():
        db.session.add(
            PingResult(
                target_host="h",
                timestamp=utcnow(),
                response_time=5.0,
                packet_loss=False,
                error_message=None,
            )
        )
        db.session.commit()
    result = CliRunner().invoke(cli, ["status", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list) and data[0]["target_host"] == "h"


def test_status_json_empty_is_array(tmp_path, monkeypatch):
    app = _make_web_app(tmp_path / "s2.db")
    monkeypatch.setattr(cli_module, "create_app", lambda *a, **k: app)
    result = CliRunner().invoke(cli, ["status", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == []  # empty set is [], not the human line


def test_ping_json_outputs_samples(tmp_path, monkeypatch):
    app = _make_web_app(tmp_path / "p.db")
    monkeypatch.setattr(cli_module, "create_app", lambda *a, **k: app)
    monkeypatch.setattr(PingService, "ping_host", _fake_ping)
    monkeypatch.setattr("time.sleep", lambda *_a: None)
    result = CliRunner().invoke(cli, ["ping", "h", "--count", "2", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data) == 2 and all(d["target_host"] == "h" for d in data)
    assert "Pinging" not in result.output  # human header suppressed in JSON mode


# --------------------------------------------------------------------------- F5


def test_export_streams_csv(tmp_path):
    app = _make_web_app(tmp_path / "e.db")
    now = utcnow()
    with app.app_context():
        db.session.add_all(
            [
                PingResult(
                    target_host="h",
                    timestamp=now,
                    response_time=5.0,
                    packet_loss=False,
                    error_message=None,
                ),
                PingResult(
                    target_host="h",
                    timestamp=now,
                    response_time=None,
                    packet_loss=True,
                    error_message="timeout",
                ),
            ]
        )
        db.session.commit()
    resp = app.test_client().get("/api/export?host=h")
    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"
    assert "attachment" in resp.headers["Content-Disposition"]
    lines = resp.get_data(as_text=True).strip().splitlines()
    assert lines[0] == "timestamp_utc,response_time_ms,packet_loss,error_message"
    assert len(lines) == 3  # header + 2 rows
    assert "+00:00" in lines[1]  # tz-aware timestamp
    assert any(",,1,timeout" in ln for ln in lines)  # loss row: empty rtt, loss=1


def test_export_requires_host_and_rejects_other_formats(tmp_path):
    app = _make_web_app(tmp_path / "e2.db")
    client = app.test_client()
    assert client.get("/api/export").status_code == 400
    assert client.get("/api/export?host=h&format=xml").status_code == 400


def test_metrics_prometheus_exposition(tmp_path):
    app = _make_web_app(tmp_path / "m.db")
    now = utcnow()
    with app.app_context():
        db.session.add_all(
            [
                PingResult(
                    target_host="h1",
                    timestamp=now,
                    response_time=10.0,
                    packet_loss=False,
                    error_message=None,
                ),
                PingResult(
                    target_host="h1",
                    timestamp=now,
                    response_time=20.0,
                    packet_loss=False,
                    error_message=None,
                ),
                PingResult(
                    target_host="h2",
                    timestamp=now,
                    response_time=None,
                    packet_loss=True,
                    error_message="x",
                ),
            ]
        )
        db.session.commit()
    resp = app.test_client().get("/metrics")
    assert resp.status_code == 200
    assert resp.mimetype == "text/plain"
    body = resp.get_data(as_text=True)
    assert 'network_ping_up{host="h1"} 1' in body
    assert 'network_ping_up{host="h2"} 0' in body
    assert 'network_ping_rtt_ms{host="h1"} 15.0' in body
    assert 'network_ping_loss_ratio{host="h2"} 1.0' in body
