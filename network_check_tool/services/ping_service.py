from typing import Dict, List, Optional, Sequence

from netdiag.measure import PingStats, ping_many

from ..models import PingResult, db
from ..utils import utcnow


def _result(host: str, timestamp, rtt: Optional[float], error: Optional[str]) -> Dict:
    return {
        "host": host,
        "timestamp": timestamp,
        "response_time": rtt,
        "packet_loss": rtt is None,
        "error_message": error,
    }


def _from_stats(host: str, timestamp, st: Optional[PingStats]) -> Dict:
    if st is None or st.error:
        return _result(host, timestamp, None, st.error if st else "not measured")
    if st.rtt is None:
        return _result(host, timestamp, None, "Request timeout")
    return _result(host, timestamp, st.rtt.avg, None)


class PingService:
    def __init__(self, timeout: float = 4.0):
        self.timeout = timeout

    def ping_hosts(self, hosts: Sequence[str]) -> List[Dict]:
        """Ping every host once, all at the same time; one result dict per host.

        The measurement comes from netdiag's shared ping core (microsecond timing
        via an ICMP socket or the Windows ICMP API when available, else the system
        ``ping`` in parallel; a missing ``ping`` comes back as the error). Each
        dict has the keys ``host``, ``timestamp``, ``response_time``
        (milliseconds, or ``None`` on failure), ``packet_loss`` (``True`` when no
        reply was received), and ``error_message``; results keep the order of
        *hosts*.
        """
        timestamp = utcnow()
        try:
            stats = ping_many(hosts, count=1, timeout=self.timeout)
        except Exception as e:
            return [_result(h, timestamp, None, str(e)) for h in hosts]
        return [_from_stats(h, timestamp, stats.get(h)) for h in hosts]

    def ping_host(self, host: str) -> Dict:
        """Measure one ping to ``host`` (see :meth:`ping_hosts`)."""
        return self.ping_hosts([host])[0]

    def save_ping_results(self, results: Sequence[Dict]) -> List[PingResult]:
        """Add result dicts to the session (the caller commits)."""
        rows = [
            PingResult(
                target_host=r["host"],
                timestamp=r["timestamp"],
                response_time=r["response_time"],
                packet_loss=r["packet_loss"],
                error_message=r["error_message"],
            )
            for r in results
        ]
        db.session.add_all(rows)
        return rows
