"""Tests for issue #31 (M10 phase 4): CLI command coverage.

Covers the core commands of both entry points via click's CliRunner: the
``netdiag`` CLI's deterministic, no-network commands, and the ``network-checker``
CLI's database commands (run against a throwaway SQLite DB).
"""

import re

from click.testing import CliRunner
from flask import Flask

import network_check_tool.cli as nct_cli
from netdiag.cli import cli as netdiag_cli
from network_check_tool.cli import cli as nct_cli_group
from network_check_tool.models import PingResult, db
from network_check_tool.utils import utcnow

# ===================================================================== netdiag


def test_netdiag_version():
    result = CliRunner().invoke(netdiag_cli, ["--version"])
    assert result.exit_code == 0, result.output
    # Don't pin the number (installed dist vs in-tree __version__ differ); just
    # require a semver-like token.
    assert re.search(r"\d+\.\d+\.\d+", result.output)


def test_netdiag_help_lists_commands():
    result = CliRunner().invoke(netdiag_cli, ["--help"])
    assert result.exit_code == 0
    assert "explain" in result.output
    assert "list-probes" in result.output


def test_netdiag_list_probes():
    result = CliRunner().invoke(netdiag_cli, ["list-probes"])
    assert result.exit_code == 0, result.output
    assert "gateway" in result.output


def test_netdiag_explain_no_arg_lists():
    result = CliRunner().invoke(netdiag_cli, ["explain"])
    assert result.exit_code == 0, result.output
    assert "Available checks" in result.output


def test_netdiag_explain_known_probe():
    result = CliRunner().invoke(netdiag_cli, ["explain", "gateway"])
    assert result.exit_code == 0, result.output
    assert "What:" in result.output
    assert "Why:" in result.output


def test_netdiag_explain_unknown_probe():
    result = CliRunner().invoke(netdiag_cli, ["explain", "no_such_probe"])
    assert result.exit_code == 1
    # The message is written to stderr; depending on the click version it surfaces
    # in .output (combined) or .stderr (separated) — accept either.
    streams = result.output
    if getattr(result, "stderr_bytes", None):
        streams += result.stderr
    assert "unknown probe" in streams


# ============================================================= network-checker


def _nct_app(db_path):
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


def _seed_one(app):
    with app.app_context():
        db.session.add(
            PingResult(
                target_host="h",
                timestamp=utcnow(),
                response_time=1.0,
                packet_loss=False,
                error_message=None,
            )
        )
        db.session.commit()


def test_init_creates_database(tmp_path, monkeypatch):
    app = _nct_app(tmp_path / "init.db")
    monkeypatch.setattr(nct_cli, "create_app", lambda *a, **k: app)

    result = CliRunner().invoke(nct_cli_group, ["init"])

    assert result.exit_code == 0, result.output
    assert "initialized successfully" in result.output
    # The table now exists: a count() succeeds (would raise if not created).
    with app.app_context():
        assert PingResult.query.count() == 0


def test_clear_confirmed_deletes_rows(tmp_path, monkeypatch):
    app = _nct_app(tmp_path / "clear.db")
    with app.app_context():
        db.create_all()
    _seed_one(app)
    monkeypatch.setattr(nct_cli, "create_app", lambda *a, **k: app)

    result = CliRunner().invoke(nct_cli_group, ["clear"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "All ping data cleared" in result.output
    with app.app_context():
        assert PingResult.query.count() == 0


def test_clear_aborted_keeps_rows(tmp_path, monkeypatch):
    app = _nct_app(tmp_path / "clear_abort.db")
    with app.app_context():
        db.create_all()
    _seed_one(app)
    monkeypatch.setattr(nct_cli, "create_app", lambda *a, **k: app)

    result = CliRunner().invoke(nct_cli_group, ["clear"], input="n\n")

    assert result.exit_code != 0  # click aborts on a declined confirmation
    with app.app_context():
        assert PingResult.query.count() == 1
