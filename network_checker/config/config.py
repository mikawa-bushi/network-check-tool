import os
from pathlib import Path

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key'
    
    # Database configuration
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///network_checker.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # Ping configuration
    PING_TIMEOUT = float(os.environ.get('PING_TIMEOUT', '4.0'))
    DEFAULT_PING_INTERVAL = int(os.environ.get('DEFAULT_PING_INTERVAL', '60'))
    
    # Default hosts to monitor
    DEFAULT_HOSTS = os.environ.get('DEFAULT_HOSTS', 'google.com,cloudflare.com').split(',')
    
    # Application directory
    APP_DIR = Path(__file__).parent.parent
    INSTANCE_PATH = APP_DIR.parent / 'instance'
    
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
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig
}