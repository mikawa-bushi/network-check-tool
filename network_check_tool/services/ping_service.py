from typing import Dict

from netdiag.core.util import have
from netdiag.probes.icmp import ping as _measure_ping

from ..models import PingResult, db
from ..utils import utcnow


class PingService:
    def __init__(self, timeout: float = 4.0):
        self.timeout = timeout

    def ping_host(self, host: str) -> Dict:
        """Measure one ping to ``host`` and return a result dict.

        The measurement comes from netdiag's shared ``ping`` core (M1) rather than
        a separate implementation. The returned dict has the keys
        ``host``, ``timestamp``, ``response_time`` (milliseconds, or ``None`` on
        failure), ``packet_loss`` (``True`` when no reply was received), and
        ``error_message``.
        """
        timestamp = utcnow()

        if not have("ping"):
            return {
                "host": host,
                "timestamp": timestamp,
                "response_time": None,
                "packet_loss": True,
                "error_message": "ping command not available",
            }

        try:
            # One ping per sample (preserves the previous cadence/cost). netdiag's
            # ping returns the average RTT already in milliseconds. Note netdiag's
            # run() caps the whole subprocess at count*timeout+5s, so the worst-case
            # block here is a few seconds longer than self.timeout alone.
            rtt, loss, _raw = _measure_ping(host, count=1, timeout=self.timeout)
        except Exception as e:
            return {
                "host": host,
                "timestamp": timestamp,
                "response_time": None,
                "packet_loss": True,
                "error_message": str(e),
            }

        # With count=1 loss is only ever 0 or 100, so "got a reply" == success.
        # (A reply with partial loss can't occur here; if count were ever raised,
        # a measured RTT would still be reported as a successful sample.)
        if rtt is None or loss >= 100.0:
            return {
                "host": host,
                "timestamp": timestamp,
                "response_time": None,
                "packet_loss": True,
                "error_message": "Request timeout",
            }

        return {
            "host": host,
            "timestamp": timestamp,
            "response_time": rtt,
            "packet_loss": False,
            "error_message": None,
        }

    def save_ping_result(self, result: Dict, commit: bool = True) -> PingResult:
        ping_result = PingResult(
            target_host=result["host"],
            timestamp=result["timestamp"],
            response_time=result["response_time"],
            packet_loss=result["packet_loss"],
            error_message=result["error_message"],
        )

        db.session.add(ping_result)
        # ``commit=False`` lets a batch caller (e.g. ``ping --count N``) add many
        # rows and commit once. The default keeps the monitoring scheduler's
        # one-row-per-job behaviour unchanged. The returned object's attributes are
        # readable before the commit, so callers can report each result immediately.
        if commit:
            db.session.commit()
        return ping_result

    def ping_and_save(self, host: str, commit: bool = True) -> PingResult:
        result = self.ping_host(host)
        return self.save_ping_result(result, commit=commit)
