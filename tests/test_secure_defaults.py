"""Tests for issue #10 (U3): secure-by-default web server.

Covers:
* default bind is localhost; public exposure is an explicit opt-in (with a warning).
* the interactive debugger cannot be enabled on a non-loopback host.
* CORS is not all-permissive by default; allowed origins are configurable.
"""

import network_check_tool.cli as cli_module
from network_check_tool.cli import cli
from click.testing import CliRunner


class _FakeApp:
    def __init__(self):
        self.run_kwargs = None

    def run(self, **kwargs):
        self.run_kwargs = kwargs


def _patch_create_app(monkeypatch):
    fake = _FakeApp()
    monkeypatch.setattr(cli_module, "create_app", lambda *a, **k: fake)
    return fake


# ----------------------------------------------------------------- bind default


def test_run_server_defaults_to_localhost(monkeypatch):
    fake = _patch_create_app(monkeypatch)
    result = CliRunner().invoke(cli, ["run-server"])
    assert result.exit_code == 0, result.output
    assert fake.run_kwargs is not None
    assert fake.run_kwargs["host"] == "127.0.0.1"


def test_run_server_public_bind_is_opt_in_and_warns(monkeypatch):
    fake = _patch_create_app(monkeypatch)
    result = CliRunner().invoke(cli, ["run-server", "--host", "0.0.0.0"])
    assert result.exit_code == 0, result.output
    assert fake.run_kwargs["host"] == "0.0.0.0"
    assert "WARNING" in result.output.upper()


# ----------------------------------------------------------------- debug gating


def test_debug_refused_on_public_host(monkeypatch):
    fake = _patch_create_app(monkeypatch)
    result = CliRunner().invoke(cli, ["run-server", "--host", "0.0.0.0", "--debug"])
    assert result.exit_code != 0
    # The server must not have started with the debugger exposed publicly.
    assert fake.run_kwargs is None


def test_debug_allowed_on_localhost(monkeypatch):
    fake = _patch_create_app(monkeypatch)
    result = CliRunner().invoke(cli, ["run-server", "--debug"])
    assert result.exit_code == 0, result.output
    assert fake.run_kwargs["debug"] is True
    assert fake.run_kwargs["host"] == "127.0.0.1"


# ------------------------------------------------------------------------ CORS


def _make_web_app(monkeypatch, tmp_path, cors_origins=None):
    from network_check_tool.config.config import DevelopmentConfig

    monkeypatch.setattr(
        DevelopmentConfig,
        "SQLALCHEMY_DATABASE_URI",
        f"sqlite:///{tmp_path / 'secure.db'}",
    )
    monkeypatch.setattr(DevelopmentConfig, "SQLALCHEMY_ECHO", False, raising=False)
    if cors_origins is not None:
        monkeypatch.setattr(
            DevelopmentConfig, "CORS_ORIGINS", cors_origins, raising=False
        )
    from network_check_tool.app import create_app

    return create_app("development")


def test_no_permissive_cors_by_default(monkeypatch, tmp_path):
    app = _make_web_app(monkeypatch, tmp_path)
    client = app.test_client()
    resp = client.get("/api/hosts", headers={"Origin": "http://evil.example"})
    acao = resp.headers.get("Access-Control-Allow-Origin")
    assert acao != "*"
    assert acao != "http://evil.example"


def test_configured_cors_origin_allowed(monkeypatch, tmp_path):
    app = _make_web_app(
        monkeypatch, tmp_path, cors_origins=["http://allowed.example"]
    )
    client = app.test_client()
    resp = client.get("/api/hosts", headers={"Origin": "http://allowed.example"})
    assert resp.headers.get("Access-Control-Allow-Origin") == "http://allowed.example"
