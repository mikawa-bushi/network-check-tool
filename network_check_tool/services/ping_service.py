import ping3
from datetime import datetime
from typing import Dict, Optional
from ..models import db, PingResult


class PingService:
    def __init__(self, timeout: float = 4.0):
        self.timeout = timeout
        ping3.EXCEPTIONS = True

    def ping_host(self, host: str) -> Dict:
        try:
            response_time = ping3.ping(host, timeout=self.timeout)

            if response_time is None:
                return {
                    "host": host,
                    "timestamp": datetime.utcnow(),
                    "response_time": None,
                    "packet_loss": True,
                    "error_message": "Request timeout",
                }
            else:
                return {
                    "host": host,
                    "timestamp": datetime.utcnow(),
                    "response_time": response_time * 1000,  # Convert to milliseconds
                    "packet_loss": False,
                    "error_message": None,
                }

        except Exception as e:
            return {
                "host": host,
                "timestamp": datetime.utcnow(),
                "response_time": None,
                "packet_loss": True,
                "error_message": str(e),
            }

    def save_ping_result(self, result: Dict) -> PingResult:
        ping_result = PingResult(
            target_host=result["host"],
            timestamp=result["timestamp"],
            response_time=result["response_time"],
            packet_loss=result["packet_loss"],
            error_message=result["error_message"],
        )

        db.session.add(ping_result)
        db.session.commit()
        return ping_result

    def ping_and_save(self, host: str) -> PingResult:
        result = self.ping_host(host)
        return self.save_ping_result(result)
