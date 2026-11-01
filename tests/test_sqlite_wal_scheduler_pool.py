"""Tests for issue #18 (P3): SQLite WAL/busy_timeout.

P3 (Flask side; netdiag's Storage already does this under M4): SQLite
connections for the dashboard DB run in WAL mode with a non-zero busy timeout,
and the PRAGMAs are skipped for non-SQLite backends. (The scheduler worker pool
this issue also sized is gone: the monitor now pings each interval's hosts in
one concurrent sweep.)
"""

import types

from sqlalchemy import create_engine, text

from network_check_tool.app import _register_sqlite_pragmas, create_app
from network_check_tool.config.config import Config

# ----------------------------------------------------------------- WAL / pragmas


def test_register_sqlite_pragmas_sets_wal_and_busy_timeout(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'wal.db'}")
    attached = _register_sqlite_pragmas(engine, 5000)
    assert attached is True

    with engine.connect() as conn:
        journal = conn.execute(text("PRAGMA journal_mode")).scalar()
        busy = conn.execute(text("PRAGMA busy_timeout")).scalar()

    assert str(journal).lower() == "wal"
    assert busy == 5000


def test_register_sqlite_pragmas_skips_non_sqlite():
    # A stub engine whose dialect is PostgreSQL: the helper must return False
    # without attaching a listener (no driver / connection needed).
    fake_engine = types.SimpleNamespace(
        dialect=types.SimpleNamespace(name="postgresql")
    )
    assert _register_sqlite_pragmas(fake_engine, 5000) is False


def test_create_app_enables_wal_on_sqlite(tmp_path, monkeypatch):
    # Point the app at a throwaway DB so create_all doesn't write into the repo.
    # DevelopmentConfig inherits SQLALCHEMY_DATABASE_URI from Config.
    monkeypatch.setattr(
        Config, "SQLALCHEMY_DATABASE_URI", f"sqlite:///{tmp_path / 'app.db'}"
    )
    app = create_app("development")
    with app.app_context():
        from network_check_tool.models import db

        with db.engine.connect() as conn:
            journal = conn.execute(text("PRAGMA journal_mode")).scalar()
    assert str(journal).lower() == "wal"


# ------------------------------------------------------------------- config knobs


def test_config_exposes_p3_tunables():
    assert isinstance(Config.SQLITE_BUSY_TIMEOUT_MS, int)
    assert Config.SQLITE_BUSY_TIMEOUT_MS > 0
