"""Tests for the device monitor (``SchedulerService``).

Hosts sharing an interval are pinged together in one sweep, the sweep's rows are
written in one transaction (from the sweep's own thread, which needs its own
Flask app context), and then each host's alerter sees its sample.
"""

import time

from flask import Flask

from network_check_tool.config.hosts import HostConfig
from network_check_tool.models import PingResult, db
from network_check_tool.services import SchedulerService
from network_check_tool.services.ping_service import _result
from network_check_tool.utils import utcnow


def _make_app(db_path):
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
    return app


class _FakePing:
    """Stands in for PingService.ping_hosts; hosts in *down* get no reply."""

    def __init__(self, down=()):
        self.down = set(down)
        self.calls = []

    def __call__(self, hosts):
        self.calls.append(list(hosts))
        now = utcnow()
        return [
            (
                _result(h, now, None, "Request timeout")
                if h in self.down
                else _result(h, now, 1.5, None)
            )
            for h in hosts
        ]


def _rows(app, host):
    with app.app_context():
        return PingResult.query.filter_by(target_host=host).count()


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_sweep_fires_and_saves_row(tmp_path):
    """A sweep runs in its own thread right after start and persists a row.

    127.0.0.1 keeps it offline-safe. Even without ICMP privileges a failed ping
    still records a packet-loss row, so this asserts the persistence /
    app-context wiring works -- not that ICMP itself succeeds.
    """
    app = _make_app(tmp_path / "fire.db")
    scheduler = SchedulerService(app=app)
    scheduler.start()  # start first, then add -- the start-monitoring order
    try:
        scheduler.add_ping_jobs([HostConfig(name="127.0.0.1", interval=60)])
        job = scheduler.list_jobs()[0]
        assert (job["host"], job["interval"]) == ("127.0.0.1", 60)
        assert job["next_run_time"]
        assert _wait_for(lambda: _rows(app, "127.0.0.1") > 0)
    finally:
        scheduler.shutdown()


def test_a_device_list_is_measured_together_from_the_first_sweep(tmp_path):
    app = _make_app(tmp_path / "one.db")
    scheduler = SchedulerService(app=app)
    fake = _FakePing(down={"b"})
    scheduler.ping_service.ping_hosts = fake
    scheduler.start()
    try:
        scheduler.add_ping_jobs(
            [HostConfig(name=h, interval=60) for h in ("a", "b", "c")]
            + [HostConfig(name="slow", interval=300)]
        )
        assert _wait_for(lambda: len(fake.calls) == 2)
    finally:
        scheduler.shutdown()

    # One call per interval; an unreachable host can't hold up the rest.
    assert sorted(fake.calls) == [["a", "b", "c"], ["slow"]]
    with app.app_context():
        rows = {r.target_host: r.packet_loss for r in PingResult.query.all()}
    assert rows == {"a": False, "b": True, "c": False, "slow": False}


def test_sweep_feeds_each_hosts_alerter(tmp_path, recording_sink):
    app = _make_app(tmp_path / "alert.db")
    scheduler = SchedulerService(app=app, sink=recording_sink)
    scheduler.ping_service.ping_hosts = _FakePing(down={"b"})
    scheduler.add_ping_jobs([HostConfig(name=h, interval=60) for h in "ab"])

    scheduler._sweep(60)
    scheduler._sweep(60)  # second consecutive loss confirms the outage

    assert [(e.event, e.host) for e in recording_sink.events] == [("degraded", "b")]


def test_hosts_that_go_down_together_are_diagnosed_in_one_run(
    tmp_path, recording_sink, monkeypatch
):
    import netdiag.runner as runner

    runs = []

    def fake_diagnose_targets(hosts, config=None):
        runs.append(list(hosts))
        return {h: None for h in hosts}

    monkeypatch.setattr(runner, "diagnose_targets", fake_diagnose_targets)
    app = _make_app(tmp_path / "diag.db")
    scheduler = SchedulerService(app=app, sink=recording_sink, diagnose=True)
    scheduler.ping_service.ping_hosts = _FakePing(down={"a", "b"})
    scheduler.add_ping_jobs([HostConfig(name=h, interval=60) for h in "abc"])

    scheduler._sweep(60)
    scheduler._sweep(60)

    assert runs == [["a", "b"]]
    assert {e.host for e in recording_sink.events} == {"a", "b"}


def test_without_sink_no_alerter_is_attached():
    scheduler = SchedulerService()
    scheduler.add_ping_jobs([HostConfig(name="127.0.0.1", interval=3600)])
    assert scheduler._targets["127.0.0.1"].alerter is None


def test_moving_or_removing_the_last_host_drops_its_sweep(tmp_path, idle_monitor):
    scheduler = SchedulerService(app=_make_app(tmp_path / "move.db"))
    scheduler.add_ping_jobs([HostConfig(name="a", interval=60)])
    assert scheduler._sweeps == {}  # nothing runs until started
    scheduler.start()
    try:
        scheduler.add_ping_jobs([HostConfig(name="b", interval=60)])
        scheduler.add_ping_jobs([HostConfig(name="a", interval=30)])
        assert sorted(scheduler._sweeps) == [30, 60]
        assert {j["host"]: j["interval"] for j in scheduler.list_jobs()} == {
            "a": 30,
            "b": 60,
        }

        scheduler.remove_ping_job("b")
        assert sorted(scheduler._sweeps) == [30]
        scheduler.remove_ping_job("a")
        scheduler.remove_ping_job("never-added")  # no-op
        assert scheduler._sweeps == {} and scheduler.list_jobs() == []
    finally:
        scheduler.shutdown()


def test_can_restart_after_shutdown(tmp_path, idle_monitor):
    scheduler = SchedulerService(app=_make_app(tmp_path / "restart.db"))
    scheduler.add_ping_jobs([HostConfig(name="127.0.0.1", interval=1)])
    scheduler.start()
    scheduler.shutdown()
    assert scheduler.list_jobs()[0]["next_run_time"] is None  # stopped

    scheduler.start()
    try:
        assert scheduler._sweeps[1]._thread.is_alive()
    finally:
        scheduler.shutdown()
