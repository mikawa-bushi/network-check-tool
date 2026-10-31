"""Line board: device names/groups, per-device status, stored alerts, ja/en UI."""

from datetime import timedelta

import pytest
from click.testing import CliRunner
from flask import Flask

from netdiag.alerting import Transition
from network_check_tool import cli as cli_module
from network_check_tool.app import create_app
from network_check_tool.config.config import Config, DevelopmentConfig
from network_check_tool.config.hosts import HostConfig
from network_check_tool.models import (
    AlertEvent,
    MonitoredHost,
    PingResult,
    db,
    upsert_hosts,
)
from network_check_tool.services.alerting import DbAlertSink, HostAlerter
from network_check_tool.utils import utcnow


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(
        Config, "SQLALCHEMY_DATABASE_URI", f"sqlite:///{tmp_path / 'board.db'}"
    )
    monkeypatch.setattr(DevelopmentConfig, "SQLALCHEMY_ECHO", False)
    return create_app("development")


def _pings(
    app, host, samples, end_ago=timedelta(seconds=5), step=timedelta(seconds=60)
):
    """*samples* oldest first: an RTT in ms, or None for a lost ping."""
    end = utcnow() - end_ago
    with app.app_context():
        for i, rtt in enumerate(samples):
            db.session.add(
                PingResult(
                    target_host=host,
                    timestamp=end - step * (len(samples) - 1 - i),
                    response_time=rtt,
                    packet_loss=rtt is None,
                )
            )
        db.session.commit()


LINE = [
    HostConfig(name="192.168.3.39", label="PLC-1", group="1ライン", note="メインPLC"),
    HostConfig(name="192.168.3.40", label="HMI-1", group="1ライン"),
    HostConfig(
        name="192.168.3.41", label="ロボット-1", group="1ライン", rtt_warn_ms=50
    ),
    HostConfig(name="192.168.3.50", label="IoTゲートウェイ", group="共通"),
    HostConfig(name="192.168.3.60", label="予備", group="共通"),
]


def _overview(app, hours=1):
    resp = app.test_client().get(f"/api/overview?hours={hours}")
    assert resp.status_code == 200
    data = resp.get_json()
    return data, {h["host"]: h for h in data["hosts"]}


def test_each_device_gets_a_status_in_list_order(app):
    with app.app_context():
        upsert_hosts(LINE)
    _pings(app, "192.168.3.39", [1.0, 1.2, 0.9])  # healthy
    _pings(app, "192.168.3.40", [1.0, None, None])  # two lost in a row: down
    _pings(app, "192.168.3.41", [1.0, 80.0])  # last reply over 50 ms: warn
    _pings(app, "192.168.3.50", [None, 2.0, 2.0])  # one loss in the window: warn
    _pings(app, "10.0.0.9", [3.0])  # pinged but not in the list: appended
    # 192.168.3.60 has no data at all: unknown

    data, by_host = _overview(app)
    assert [h["host"] for h in data["hosts"]] == [d.name for d in LINE] + ["10.0.0.9"]
    status = {h: v["status"] for h, v in by_host.items()}
    assert status == {
        "192.168.3.39": "ok",
        "192.168.3.40": "down",
        "192.168.3.41": "warn",
        "192.168.3.50": "warn",
        "192.168.3.60": "unknown",
        "10.0.0.9": "ok",
    }
    assert data["counts"] == {"down": 1, "warn": 2, "ok": 2, "unknown": 1}
    plc = by_host["192.168.3.39"]
    assert (plc["name"], plc["group"], plc["note"]) == ("PLC-1", "1ライン", "メインPLC")
    assert (
        plc["total"] == 3
        and plc["lost"] == 0
        and plc["avg_rtt_ms"] == pytest.approx(1.0333, abs=1e-3)
    )
    down = by_host["192.168.3.40"]
    assert down["last_loss"] is True and down["last_ok_ts"] is not None
    assert by_host["192.168.3.50"]["loss_pct"] == pytest.approx(100 / 3)
    # An amber tile says why: losses in the window vs. a slow last reply.
    assert by_host["192.168.3.50"]["reasons"] == ["loss"]
    assert (
        by_host["192.168.3.41"]["reasons"] == ["slow"]
        and by_host["192.168.3.41"]["rtt_warn_ms"] == 50
    )


def test_silent_monitor_is_unknown_not_ok(app):
    with app.app_context():
        upsert_hosts([HostConfig(name="192.168.3.39", label="PLC-1", interval=60)])
    _pings(app, "192.168.3.39", [1.0, 1.0], end_ago=timedelta(minutes=30))
    _, by_host = _overview(app, hours=24)
    assert by_host["192.168.3.39"]["status"] == "unknown"
    assert by_host["192.168.3.39"]["stale"] is True


def test_device_list_is_authoritative_ui_additions_append(app):
    with app.app_context():
        upsert_hosts(LINE[:2])
        upsert_hosts([HostConfig(name="10.0.0.1", label="追加機器")], from_list=False)
        upsert_hosts([LINE[1], LINE[0]])  # re-imported list in a new order
        rows = db.session.query(MonitoredHost).order_by(MonitoredHost.position).all()
        assert [r.host for r in rows] == ["192.168.3.40", "192.168.3.39", "10.0.0.1"]
        assert rows[2].name == "追加機器"


def test_alerts_are_stored_with_the_diagnosis(app, monkeypatch):
    import netdiag.runner as runner

    class _Hyp:
        cause = "gateway"
        confidence = "high"
        summary = "ゲートウェイが応答しません"
        runbook = ["ケーブルを確認"]

    monkeypatch.setattr(
        runner,
        "diagnose_targets",
        lambda hosts, config=None: {h: _Hyp() for h in hosts},
    )
    with app.app_context():
        upsert_hosts(LINE[:1])
    alerter = HostAlerter(
        "192.168.3.39",
        DbAlertSink(app),
        fail_threshold=1,
        recover_threshold=1,
        diagnose=True,
    )
    assert alerter.observe(packet_loss=True, response_time=None).event == "degraded"
    _pings(app, "192.168.3.39", [1.0, None, None])

    _, by_host = _overview(app)
    alert = by_host["192.168.3.39"]["alert"]
    assert alert["event"] == "degraded" and alert["cause"] == "gateway"
    assert alert["diagnosis"] == "ゲートウェイが応答しません" and alert["runbook"] == [
        "ケーブルを確認"
    ]

    alerter.observe(packet_loss=False, response_time=1.0)  # recovered
    events = app.test_client().get("/api/alerts?hours=1").get_json()
    assert [e["event"] for e in events] == ["recovered", "degraded"]
    assert events[0]["name"] == "PLC-1" and events[0]["duration_s"] is not None


def test_alert_summary_follows_the_language(ja):
    sink = []
    alerter = HostAlerter(
        "10.0.0.1",
        type("S", (), {"emit": lambda self, t: sink.append(t)})(),
        fail_threshold=1,
    )
    alerter.observe(packet_loss=True, response_time=None)
    assert sink[0].summary == "10.0.0.1: 応答なし" and sink[0].host == "10.0.0.1"


def test_undiagnosed_alert_keeps_no_fake_cause(app):
    t = Transition(
        event="degraded",
        from_state="healthy",
        to_state="degraded",
        ts=1_760_000_000.0,
        cause="10.0.0.1",
        summary="10.0.0.1: packet loss",
        host="10.0.0.1",
    )
    DbAlertSink(app).emit(t)
    with app.app_context():
        row = db.session.query(AlertEvent).one()
        assert row.host == "10.0.0.1" and row.cause is None and row.confidence is None


def test_alerts_rejects_bad_parameters(app):
    client = app.test_client()
    assert client.get("/api/alerts?hours=x").status_code == 400
    assert client.get("/api/alerts?limit=x").status_code == 400
    assert client.get("/api/overview?hours=x").status_code == 400


def test_page_language_follows_query_then_browser(app):
    client = app.test_client()
    assert 'lang="ja"' in client.get("/").get_data(
        as_text=True
    )  # no preference: Japanese
    assert 'lang="en"' in client.get(
        "/", headers={"Accept-Language": "en-US,en;q=0.9"}
    ).get_data(as_text=True)
    assert 'lang="ja"' in client.get(
        "/?lang=ja", headers={"Accept-Language": "en-US"}
    ).get_data(as_text=True)
    body = client.get("/").get_data(as_text=True)
    assert "ライン一覧" in body and "/api/overview" in body


def test_chart_speaks_the_page_language_and_uses_the_device_name(app):
    with app.app_context():
        upsert_hosts(LINE[:1])
    _pings(app, "192.168.3.39", [1.0, 2.0])
    fig = (
        app.test_client()
        .get("/api/ping_chart?host=192.168.3.39&hours=1&lang=ja")
        .get_json()
    )
    assert "PLC-1 (192.168.3.39) の応答" in str(fig["layout"]["title"])
    assert {tr["name"] for tr in fig["data"]} == {"応答時間 (ms)"}


@pytest.fixture
def sched_app(tmp_path, monkeypatch, idle_monitor):
    monkeypatch.setattr(
        Config, "SQLALCHEMY_DATABASE_URI", f"sqlite:///{tmp_path / 'crud.db'}"
    )
    monkeypatch.setattr(DevelopmentConfig, "SQLALCHEMY_ECHO", False)
    application = create_app("development", register_scheduler=True)
    yield application
    application.extensions["scheduler"].shutdown()


def test_ui_added_host_carries_name_and_group_until_removed(sched_app):
    client = sched_app.test_client()
    created = client.post(
        "/api/hosts",
        json={
            "host": "10.0.0.5",
            "interval": 3600,
            "name": "検査機",
            "group": "2ライン",
        },
    )
    assert created.status_code == 201
    assert created.get_json() == {
        "host": "10.0.0.5",
        "interval": 3600,
        "name": "検査機",
        "group": "2ライン",
    }
    _, by_host = _overview(sched_app)
    assert (
        by_host["10.0.0.5"]["name"] == "検査機"
        and by_host["10.0.0.5"]["status"] == "unknown"
    )
    target = sched_app.extensions["scheduler"]._targets["10.0.0.5"]
    assert isinstance(target.alerter, HostAlerter)  # outages are recorded for the board
    client.delete("/api/hosts/10.0.0.5")
    assert _overview(sched_app)[0]["hosts"] == []


def test_run_server_devices_labels_and_monitors_the_line(
    tmp_path, monkeypatch, idle_monitor
):
    devices = tmp_path / "line.csv"
    devices.write_bytes(
        "名前,IPアドレス,グループ\r\nPLC-1,192.168.3.39,1ライン\r\n".encode("cp932")
    )
    monkeypatch.setattr(
        Config, "SQLALCHEMY_DATABASE_URI", f"sqlite:///{tmp_path / 'rs.db'}"
    )
    made = {}

    def fake_create_app(*a, **kw):
        made["app"] = create_app(*a, **kw)
        return made["app"]

    monkeypatch.setattr(cli_module, "create_app", fake_create_app)
    monkeypatch.setattr(Flask, "run", lambda self, **kw: None)
    try:
        result = CliRunner().invoke(
            cli_module.cli, ["run-server", "--devices", str(devices)]
        )
        assert result.exit_code == 0, result.output
        app = made["app"]
        with app.app_context():
            row = db.session.get(MonitoredHost, "192.168.3.39")
            assert (row.name, row.group_name) == ("PLC-1", "1ライン")
        monitored = app.extensions["scheduler"].list_jobs()
        assert [j["host"] for j in monitored] == ["192.168.3.39"]
    finally:
        if "app" in made:
            made["app"].extensions["scheduler"].shutdown()


def test_run_server_rejects_a_broken_device_list(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("foo,bar\n1,2\n", encoding="utf-8")
    result = CliRunner().invoke(cli_module.cli, ["run-server", "--devices", str(bad)])
    assert result.exit_code == 1 and "invalid device list" in result.output
