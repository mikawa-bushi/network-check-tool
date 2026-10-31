import os
from pathlib import Path


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY") or "dev-secret-key"

    # Database configuration
    SQLALCHEMY_DATABASE_URI = (
        os.environ.get("DATABASE_URL") or "sqlite:///network_check_tool.db"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # SQLite tuning (ignored for non-SQLite DATABASE_URL backends). Matches
    # netdiag.storage: WAL plus a non-zero busy timeout so a dashboard reader and
    # the monitoring writer don't collide into an immediate "database is locked".
    SQLITE_BUSY_TIMEOUT_MS = int(os.environ.get("SQLITE_BUSY_TIMEOUT_MS", "5000"))

    # Ping configuration
    PING_TIMEOUT = float(os.environ.get("PING_TIMEOUT", "4.0"))
    DEFAULT_PING_INTERVAL = int(os.environ.get("DEFAULT_PING_INTERVAL", "60"))

    # Cross-origin access for the data API. Comma-separated list of allowed
    # origins (e.g. "https://dash.example"). Empty (default) means no
    # cross-origin access is granted -- same-origin requests are unaffected.
    # NOTE: evaluated at import time (like the other settings here), so the
    # CORS_ORIGINS env var must be set before this module is first imported.
    CORS_ORIGINS = [
        o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()
    ]

    # Default hosts to monitor
    DEFAULT_HOSTS = os.environ.get("DEFAULT_HOSTS", "google.com,cloudflare.com").split(
        ","
    )

    # Application directory
    APP_DIR = Path(__file__).parent.parent
    INSTANCE_PATH = APP_DIR.parent / "instance"

    @staticmethod
    def init_app(app):
        """Initialize application with configuration"""
        # Ensure instance directory exists
        Config.INSTANCE_PATH.mkdir(exist_ok=True)


class DevelopmentConfig(Config):
    DEBUG = True
    SQLALCHEMY_ECHO = True


class ProductionConfig(Config):
    DEBUG = False


config = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig,
}
