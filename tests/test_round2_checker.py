"""Round-2 review features (network-checker side).

F8 -- ``network-checker status``/``ping`` gain a ``--json`` machine-readable mode.
F5 -- dashboard ``/api/export`` (CSV) and ``/metrics`` (Prometheus).
F2 -- ``start-monitoring --config`` loads hosts with per-host interval/thresholds.
"""

import json

import pytest
from click.testing import CliRunner
from conftest import RecordingSink
from flask import Flask

from network_check_tool import cli as cli_module
from network_check_tool.cli import cli
from network_check_tool.config.hosts import load_hosts
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


# --------------------------------------------------------------------------- F2


def test_load_hosts_toml_per_host_fields(tmp_path):
    p = tmp_path / "hosts.toml"
    p.write_text(
        "[[host]]\n"
        'name = "google.com"\n'
        "interval = 30\n"
        "rtt_warn_ms = 200\n"
        "loss_warn_pct = 5\n\n"
        "[[host]]\n"
        'name = "8.8.8.8"\n'
    )
    hosts = load_hosts(p, default_interval=60)
    assert [h.name for h in hosts] == ["google.com", "8.8.8.8"]
    assert hosts[0].interval == 30
    assert hosts[0].rtt_warn_ms == 200
    assert hosts[0].loss_warn_pct == 5
    assert hosts[1].interval == 60  # default applied


def test_load_hosts_json_and_bare_strings(tmp_path):
    p = tmp_path / "hosts.json"
    p.write_text(json.dumps({"host": ["a.com", {"name": "b.com", "interval": 10}]}))
    hosts = load_hosts(p, default_interval=45)
    assert (hosts[0].name, hosts[0].interval) == ("a.com", 45)
    assert (hosts[1].name, hosts[1].interval) == ("b.com", 10)


def test_load_hosts_rejects_malformed(tmp_path):
    missing_name = tmp_path / "bad.toml"
    missing_name.write_text("[[host]]\ninterval = 5\n")
    with pytest.raises(ValueError):
        load_hosts(missing_name)

    no_hosts = tmp_path / "empty.json"
    no_hosts.write_text(json.dumps({"other": []}))
    with pytest.raises(ValueError):
        load_hosts(no_hosts)


def _raise_keyboard_interrupt(*_a):
    raise KeyboardInterrupt


def test_start_monitoring_config_wires_per_host_interval(tmp_path, monkeypatch):
    cfg = tmp_path / "hosts.toml"
    cfg.write_text(
        '[[host]]\nname="a"\ninterval=30\n\n[[host]]\nname="b"\ninterval=90\n'
    )
    added = []

    class _FakeScheduler:
        def __init__(self, **kw):
            pass

        def start(self):
            pass

        def add_ping_jobs(self, configs):
            added.extend((hc.name, hc.interval) for hc in configs)

        def shutdown(self):
            pass

    app = _make_web_app(tmp_path / "sm.db")
    monkeypatch.setattr(cli_module, "SchedulerService", _FakeScheduler)
    monkeypatch.setattr(cli_module, "create_app", lambda *a, **k: app)
    monkeypatch.setattr("time.sleep", _raise_keyboard_interrupt)  # break the wait loop

    result = CliRunner().invoke(cli, ["start-monitoring", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert added == [("a", 30), ("b", 90)]


def test_start_monitoring_requires_hosts_or_config():
    result = CliRunner().invoke(cli, ["start-monitoring"])
    assert result.exit_code != 0
    assert "provide one or more HOSTS or --config" in result.output


# --------------------------------------------------------------------------- F3


def test_host_alerter_degrades_after_consecutive_loss():
    from network_check_tool.services.alerting import HostAlerter

    sink = RecordingSink()
    alerter = HostAlerter("h", sink, fail_threshold=2, recover_threshold=2)
    assert alerter.observe(packet_loss=True, response_time=None) is None  # 1 bad
    t = alerter.observe(packet_loss=True, response_time=None)  # 2 bad -> DEGRADED
    assert t is not None and t.event == "degraded"
    assert sink.events[-1].event == "degraded"


def test_host_alerter_rtt_threshold_and_healthy(monkeypatch):
    from network_check_tool.services.alerting import HostAlerter

    over = HostAlerter("h", RecordingSink(), rtt_warn_ms=100, fail_threshold=1)
    assert over.observe(packet_loss=False, response_time=250.0).event == "degraded"

    sink = RecordingSink()
    under = HostAlerter("h", sink, rtt_warn_ms=100, fail_threshold=1)
    assert under.observe(packet_loss=False, response_time=50.0) is None
    assert sink.events == []  # healthy -> no alert


def test_host_alerter_recovers():
    from network_check_tool.services.alerting import HostAlerter

    sink = RecordingSink()
    alerter = HostAlerter("h", sink, fail_threshold=1, recover_threshold=1)
    alerter.observe(packet_loss=True, response_time=None)  # DEGRADED
    t = alerter.observe(packet_loss=False, response_time=10.0)  # RECOVERED
    assert t is not None and t.event == "recovered"


def test_host_alerter_swallows_sink_failure():
    from network_check_tool.services.alerting import HostAlerter

    class _BadSink:
        def emit(self, transition):
            raise RuntimeError("boom")

    alerter = HostAlerter("h", _BadSink(), fail_threshold=1)
    # State still changed; the sink error is logged, never raised.
    assert alerter.observe(packet_loss=True, response_time=None) is not None


# --------------------------------------------------------------------------- F1


def _patch_fake_netdiag(monkeypatch, cause="gateway"):
    import netdiag.diagnosis as nd
    import netdiag.runner as runner

    class _Hyp:
        def __init__(self):
            self.cause = cause
            self.confidence = "high"
            self.summary = f"{cause} down"
            self.evidence = []

    seen = {}

    def fake_diagnose_targets(hosts, config=None):
        seen["hosts"] = list(hosts)
        return {h: _Hyp() for h in hosts}

    monkeypatch.setattr(runner, "diagnose_targets", fake_diagnose_targets)
    monkeypatch.setattr(nd, "runbook_steps", lambda c: [f"fix {c}"])
    return seen


def test_diagnose_attaches_root_cause_on_degraded(monkeypatch):
    from network_check_tool.services.alerting import HostAlerter

    seen = _patch_fake_netdiag(monkeypatch, cause="gateway")
    sink = RecordingSink()
    alerter = HostAlerter("h", sink, fail_threshold=1, diagnose=True)
    t = alerter.observe(packet_loss=True, response_time=None)
    assert t.event == "degraded"
    assert seen["hosts"] == ["h"]  # the degraded host itself is diagnosed
    assert t.cause == "gateway"
    assert any("netdiag root cause: gateway" in e for e in t.evidence)
    assert t.runbook == ["fix gateway"]


def test_diagnose_off_does_not_run_netdiag(monkeypatch):
    import netdiag.runner as runner
    from network_check_tool.services.alerting import HostAlerter

    def _boom(*a, **k):
        raise AssertionError("netdiag must not run when diagnose is off")

    monkeypatch.setattr(runner, "diagnose_targets", _boom)
    sink = RecordingSink()
    alerter = HostAlerter("h", sink, fail_threshold=1, diagnose=False)
    t = alerter.observe(packet_loss=True, response_time=None)
    assert t.event == "degraded" and t.cause == "h"  # unenriched


def test_start_monitoring_help_lists_diagnose():
    out = CliRunner().invoke(cli, ["start-monitoring", "--help"]).output
    assert "--diagnose" in out
    assert "--config" in out
    assert "--alert-file" in out


def test_load_hosts_accepts_a_netdiag_device_list(tmp_path):
    p = tmp_path / "line1.csv"
    p.write_bytes(
        "名前,IPアドレス,監視間隔,遅延上限\r\nPLC-1,192.168.3.39,10,5\r\nHMI,192.168.3.40,,\r\n".encode(
            "cp932"
        )
    )
    hosts = load_hosts(p, default_interval=60)
    assert [(h.name, h.label, h.interval, h.rtt_warn_ms) for h in hosts] == [
        ("192.168.3.39", "PLC-1", 10, 5.0),
        ("192.168.3.40", "HMI", 60, None),
    ]


def test_load_hosts_json_devices_key(tmp_path):
    p = tmp_path / "line1.json"
    p.write_text('{"devices": [{"name": "PLC", "host": "10.0.0.5"}]}', encoding="utf-8")
    assert load_hosts(p)[0].name == "10.0.0.5"
