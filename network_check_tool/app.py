import os

from flask import Flask
from flask_cors import CORS
from sqlalchemy import event

from .config.config import config
from .models import db
from .views.dashboard import dashboard_bp


def _register_sqlite_pragmas(engine, busy_timeout_ms):
    """Put SQLite connections from ``engine`` into WAL mode with a busy timeout.

    Attaches a ``connect`` event listener that runs ``PRAGMA journal_mode=WAL``
    and ``PRAGMA busy_timeout=<busy_timeout_ms>`` on every new DBAPI connection,
    so a dashboard reader and the monitoring writer don't collide into an
    immediate "database is locked". Does nothing for non-SQLite engines (e.g. a
    PostgreSQL ``DATABASE_URL``), where those PRAGMAs are invalid.

    Returns ``True`` if the listener was attached (SQLite), ``False`` otherwise.
    """
    if engine.dialect.name != "sqlite":
        return False

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        cursor.close()

    return True


def create_app(config_name=None, register_scheduler=False):
    """Application factory pattern.

    ``register_scheduler=True`` starts an in-process monitoring scheduler and
    attaches it at ``app.extensions['scheduler']`` so the dashboard's host-CRUD
    API can manage jobs in the same process. It defaults off because it is only
    safe in a single process: under a multi-worker WSGI server each worker would
    start its own scheduler and double up pings. ``run-server`` enables it (on a
    loopback bind only); multi-worker deployments use ``start-monitoring`` instead.
    """
    if config_name is None:
        config_name = os.environ.get("FLASK_ENV", "development")

    app = Flask(__name__)
    app.config.from_object(config[config_name])

    # Initialize extensions
    db.init_app(app)
    # Register the SQLite WAL/busy_timeout listener before the first connection is
    # opened (db.create_all below), so every pooled connection is tuned. No-op for
    # non-SQLite backends.
    with app.app_context():
        _register_sqlite_pragmas(db.engine, app.config["SQLITE_BUSY_TIMEOUT_MS"])
    # Only enable cross-origin access when origins are explicitly configured, and
    # scope it to the data API. With no CORS_ORIGINS set (default), no permissive
    # CORS headers are sent; same-origin dashboard requests are unaffected.
    cors_origins = app.config.get("CORS_ORIGINS") or []
    if cors_origins:
        CORS(app, resources={r"/api/*": {"origins": cors_origins}})

    # Register blueprints
    app.register_blueprint(dashboard_bp)

    # Create database tables
    with app.app_context():
        db.create_all()

    if register_scheduler:
        # In-process scheduler for the dashboard's host-management API (F4). Starts
        # empty; hosts are added via POST /api/hosts. Single-process only (see the
        # docstring). Attached under app.extensions so the views can reach it.
        from .services import SchedulerService
        from .services.alerting import DbAlertSink

        scheduler = SchedulerService(
            app=app,
            timeout=app.config["PING_TIMEOUT"],
            # Confirmed outages land in the DB so the line board can show them.
            sink=DbAlertSink(app),
        )
        scheduler.start()
        app.extensions["scheduler"] = scheduler

    return app
