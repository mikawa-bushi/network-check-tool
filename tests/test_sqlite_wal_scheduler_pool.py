"""Tests for issue #18 (P3): SQLite WAL/busy_timeout + right-sized scheduler pool.

P3 (Flask side; netdiag's Storage already does this under M4):
- SQLite connections for the dashboard DB run in WAL mode with a non-zero busy
  timeout, and the PRAGMAs are skipped for non-SQLite backends.
- The monitoring scheduler's worker-pool size is a small, configurable default
  rather than the old hardcoded 20.
"""

import types

from sqlalchemy import create_engine, text

from network_check_tool.app import _register_sqlite_pragmas, create_app
from network_check_tool.config.config import Config
from network_check_tool.services.scheduler_service import (
    DEFAULT_SCHEDULER_MAX_WORKERS,
    SchedulerService,
)


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


# --------------------------------------------------------------- scheduler pool


def test_scheduler_uses_configured_max_workers():
    svc = SchedulerService(max_workers=7)
    assert svc.max_workers == 7


def test_scheduler_default_pool_is_small():
    svc = SchedulerService()
    assert svc.max_workers == DEFAULT_SCHEDULER_MAX_WORKERS
    assert svc.max_workers < 20  # the old hardcoded size


# ------------------------------------------------------------------- config knobs


def test_config_exposes_p3_tunables():
    # Knobs exist and are positive ints. We deliberately do NOT bound
    # SCHEDULER_MAX_WORKERS here: an operator may export a large value, and the
    # "small default" intent is pinned against the module constant in
    # test_scheduler_default_pool_is_small instead.
    assert isinstance(Config.SQLITE_BUSY_TIMEOUT_MS, int)
    assert Config.SQLITE_BUSY_TIMEOUT_MS > 0
    assert isinstance(Config.SCHEDULER_MAX_WORKERS, int)
    assert Config.SCHEDULER_MAX_WORKERS > 0


def test_scheduler_rejects_non_positive_max_workers():
    import pytest

    with pytest.raises(ValueError):
        SchedulerService(max_workers=0)
