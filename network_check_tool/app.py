from flask import Flask
from flask_cors import CORS
from .models import db
from .config.config import config
from .views.dashboard import dashboard_bp
import os


def create_app(config_name=None):
    """Application factory pattern"""
    if config_name is None:
        config_name = os.environ.get("FLASK_ENV", "development")

    app = Flask(__name__)
    app.config.from_object(config[config_name])

    # Initialize extensions
    db.init_app(app)
    CORS(app)

    # Register blueprints
    app.register_blueprint(dashboard_bp)

    # Create database tables
    with app.app_context():
        db.create_all()

    return app
