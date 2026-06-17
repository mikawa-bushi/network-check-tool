"""Round-2 review fixes (2026-06-17): network-checker CLI.

* P2 -- ``ping --count N`` adds rows with ``commit=False`` and commits once at the
  end (one transaction for the batch) instead of committing per ping.
* U11 -- ``run-server -h`` prints help (Click's help alias) rather than being
  silently consumed as the ``--host`` short option.
"""

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
        "response_time": 1.0,
        "packet_loss": False,
        "error_message": None,
    }


# --------------------------------------------------------------------------- P2


def test_cli_ping_commits_once_for_the_batch(tmp_path, monkeypatch):
    app = _make_web_app(tmp_path / "p2.db")
    monkeypatch.setattr(cli_module, "create_app", lambda *a, **k: app)
    monkeypatch.setattr(PingService, "ping_host", _fake_ping)

    seen_commit_flags = []
    orig_save = PingService.save_ping_result

    def spy_save(self, result, commit=True):
        seen_commit_flags.append(commit)
        return orig_save(self, result, commit=commit)

    monkeypatch.setattr(PingService, "save_ping_result", spy_save)

    result = CliRunner().invoke(cli, ["ping", "example.com", "--count", "3"])

    assert result.exit_code == 0, result.output
    # Every per-ping save deferred its commit ...
    assert seen_commit_flags == [False, False, False]
    # ... and the single post-loop commit still persisted all three rows.
    with app.app_context():
        assert PingResult.query.count() == 3


def test_save_ping_result_no_commit_leaves_row_pending(tmp_path):
    app = _make_web_app(tmp_path / "p2b.db")
    svc = PingService()
    with app.app_context():
        svc.save_ping_result(
            {
                "host": "h",
                "timestamp": utcnow(),
                "response_time": 1.0,
                "packet_loss": False,
                "error_message": None,
            },
            commit=False,
        )
        # Not committed yet: a fresh session/connection sees no row.
        db.session.rollback()
        assert PingResult.query.count() == 0


# -------------------------------------------------------------------------- U11


def test_run_server_dash_h_prints_help():
    result = CliRunner().invoke(cli, ["run-server", "-h"])
    assert result.exit_code == 0
    assert "Usage:" in result.output
    assert "--host" in result.output


def test_group_dash_h_prints_help():
    result = CliRunner().invoke(cli, ["-h"])
    assert result.exit_code == 0
    assert "Usage:" in result.output
