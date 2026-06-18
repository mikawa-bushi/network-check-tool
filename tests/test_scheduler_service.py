"""Regression tests for the ping scheduler.

The dashboard's ``start-monitoring`` used to die instantly with "Schedulers
cannot be serialized": a persistent (SQLAlchemy) jobstore tried to pickle a
bound-method job whose ``self`` carried the scheduler. These tests pin the fix --
a job registers without serialization, and actually fires and writes a row into
``ping_results`` from the APScheduler worker thread (which requires its own Flask
app context).
"""

import time

from flask import Flask

from network_check_tool.models import PingResult, db
from network_check_tool.services import SchedulerService


def _make_app(db_path):
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
    return app


def test_add_ping_job_does_not_serialize(tmp_path):
    """Registering a job while the scheduler runs must not raise.

    Mirrors the real start-monitoring flow (start, then add_job) that triggered
    the serialization error under the old persistent jobstore.
    """
    app = _make_app(tmp_path / "sched.db")
    scheduler = SchedulerService(app=app)
    scheduler.start()
    try:
        scheduler.add_ping_job("127.0.0.1", interval=60)  # must not raise
        assert any(j.id == "ping_127.0.0.1" for j in scheduler.list_jobs())
    finally:
        scheduler.shutdown()


def test_ping_job_fires_and_saves_row(tmp_path):
    """A short-interval job must run in the worker thread and persist a row.

    127.0.0.1 keeps it offline-safe. Even without raw-socket ICMP privileges,
    ``ping_and_save`` still records a packet-loss row, so this asserts the
    persistence / app-context wiring works -- not that ICMP itself succeeds.
    """
    app = _make_app(tmp_path / "fire.db")
    scheduler = SchedulerService(app=app)
    scheduler.add_ping_job("127.0.0.1", interval=1)
    scheduler.start()
    try:
        deadline = time.time() + 10
        count = 0
        while time.time() < deadline:
            with app.app_context():
                count = PingResult.query.filter_by(target_host="127.0.0.1").count()
            if count > 0:
                break
            time.sleep(0.3)
    finally:
        scheduler.shutdown()

    assert count > 0, "scheduled ping job never persisted a ping_results row"
