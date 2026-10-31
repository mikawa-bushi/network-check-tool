"""Round-2 review feature F4: dashboard host-management API.

The write API (GET /api/jobs, POST /api/hosts, DELETE /api/hosts/<host>) operates
on the in-process scheduler attached when create_app(register_scheduler=True).
Without it (the default, and public/multi-worker deployments) the routes 503.
"""

import pytest
from flask import Flask

from network_check_tool.app import create_app
from network_check_tool.config.config import Config, DevelopmentConfig
from network_check_tool.models import db
from network_check_tool.views.dashboard import dashboard_bp


@pytest.fixture
def sched_app(tmp_path, monkeypatch, idle_monitor):
    monkeypatch.setattr(
        Config, "SQLALCHEMY_DATABASE_URI", f"sqlite:///{tmp_path / 'crud.db'}"
    )
    monkeypatch.setattr(DevelopmentConfig, "SQLALCHEMY_ECHO", False)
    app = create_app("development", register_scheduler=True)
    yield app
    scheduler = app.extensions.get("scheduler")
    if scheduler is not None:
        scheduler.shutdown()


def test_host_crud_flow(sched_app):
    client = sched_app.test_client()
    assert client.get("/api/jobs").get_json() == []  # starts empty

    # A long interval means the job never actually fires during the test.
    created = client.post("/api/hosts", json={"host": "example.com", "interval": 3600})
    assert created.status_code == 201
    assert created.get_json() == {"host": "example.com", "interval": 3600}

    jobs = client.get("/api/jobs").get_json()
    assert [j["host"] for j in jobs] == ["example.com"]
    assert jobs[0]["interval"] == 3600

    removed = client.delete("/api/hosts/example.com")
    assert removed.status_code == 200
    assert client.get("/api/jobs").get_json() == []


def test_add_host_validation(sched_app):
    client = sched_app.test_client()
    assert client.post("/api/hosts", json={}).status_code == 400  # missing host
    assert (
        client.post("/api/hosts", json={"host": "h", "interval": "abc"}).status_code
        == 400
    )
    assert (
        client.post("/api/hosts", json={"host": "h", "interval": 0}).status_code == 400
    )


def test_write_api_disabled_without_scheduler(tmp_path):
    # A plain app with the blueprint but no attached scheduler -> 503 everywhere.
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{tmp_path / 'ro.db'}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    app.register_blueprint(dashboard_bp)
    with app.app_context():
        db.create_all()

    client = app.test_client()
    assert client.get("/api/jobs").status_code == 503
    assert client.post("/api/hosts", json={"host": "h"}).status_code == 503
    assert client.delete("/api/hosts/h").status_code == 503
