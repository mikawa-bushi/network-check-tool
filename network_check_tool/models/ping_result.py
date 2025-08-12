from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class PingResult(db.Model):
    __tablename__ = 'ping_results'
    
    id = db.Column(db.Integer, primary_key=True)
    target_host = db.Column(db.String(255), nullable=False, index=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    response_time = db.Column(db.Float, nullable=True)
    packet_loss = db.Column(db.Boolean, default=False, nullable=False)
    error_message = db.Column(db.Text, nullable=True)
    
    def __repr__(self):
        return f'<PingResult {self.target_host} at {self.timestamp}>'
    
    def to_dict(self):
        return {
            'id': self.id,
            'target_host': self.target_host,
            'timestamp': self.timestamp.isoformat(),
            'response_time': self.response_time,
            'packet_loss': self.packet_loss,
            'error_message': self.error_message
        }