"""Device monitoring: ping every host on its interval and record the results.

Hosts that share an interval are pinged together, once per interval, through
netdiag's ``ping_many``, so a host that doesn't answer never delays or skips the
others. With the ICMP socket a sweep takes about as long as the slowest reply;
the Windows ICMP API and the system ``ping`` fallback work through a bounded
pool, so a whole line that is down takes a few timeouts. Each sweep writes its
rows in one transaction, then feeds every host's alerter; the hosts that just
went down are diagnosed together in one netdiag run.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from netdiag.periodic import PeriodicLoop

from ..config.hosts import HostConfig
from ..models import db
from .alerting import HostAlerter, report
from .ping_service import PingService

logger = logging.getLogger(__name__)


@dataclass
class _Target:
    host: str
    interval: int
    alerter: Optional[HostAlerter] = None  # set when alerting is configured


class _Sweep(PeriodicLoop):
    """Ping every host registered at one interval, once per interval."""

    _tick_error_msg = "monitoring sweep failed"

    def __init__(self, service: "SchedulerService", interval: int) -> None:
        super().__init__()
        self.service = service
        self.interval = interval

    def _tick(self) -> None:
        self.service._sweep(self.interval)


class SchedulerService:
    def __init__(
        self,
        app=None,
        timeout: Optional[float] = None,
        sink=None,
        diagnose: bool = False,
    ):
        # Flask app whose context each sweep pushes before touching the DB
        # session (sweeps run in their own threads; contexts are thread-local).
        self.app = app
        # Optional alert sink (netdiag AlertSink). When set, each host gets a
        # HostAlerter and threshold breaches emit DEGRADED/RECOVERED.
        self.sink = sink
        # When True, a confirmed degradation also runs a netdiag sweep (F1).
        self.diagnose = diagnose
        # Forward the caller-provided timeout (e.g. Config.PING_TIMEOUT, injected
        # at the CLI edge); the library layer never reads global configuration.
        self.ping_service = (
            PingService(timeout=timeout) if timeout is not None else PingService()
        )
        self._targets: Dict[str, _Target] = {}
        # One running loop per interval in use; empty while stopped.
        self._sweeps: Dict[int, _Sweep] = {}
        self._lock = threading.Lock()
        self.is_running = False

    def start(self):
        with self._lock:
            if self.is_running:
                return
            self.is_running = True
            for interval in {t.interval for t in self._targets.values()}:
                self._start_sweep(interval)
        logger.info("Monitoring started")

    def shutdown(self):
        with self._lock:
            if not self.is_running:
                return
            self.is_running = False
            sweeps, self._sweeps = self._sweeps, {}
        # Don't wait for a sweep in flight: its rows are one transaction.
        for sweep in sweeps.values():
            sweep.stop(wait=False)
        logger.info("Monitoring stopped")

    def add_ping_jobs(self, configs: Iterable[HostConfig]):
        """Monitor every host in *configs* (replacing earlier entries).

        All of them are registered before any new interval's first sweep, which
        runs straight away -- so a whole device list is measured together.
        """
        targets = [
            _Target(
                hc.name,
                hc.interval,
                (
                    HostAlerter(
                        hc.name,
                        self.sink,
                        rtt_warn_ms=hc.rtt_warn_ms,
                        diagnose=self.diagnose,
                    )
                    if self.sink is not None
                    else None
                ),
            )
            for hc in configs
        ]
        with self._lock:
            old = {
                self._targets[t.host].interval
                for t in targets
                if t.host in self._targets
            }
            self._targets.update((t.host, t) for t in targets)
            if self.is_running:
                for interval in {t.interval for t in targets} - set(self._sweeps):
                    self._start_sweep(interval)
            idle = [s for s in map(self._release, old) if s is not None]
        for sweep in idle:
            sweep.stop(wait=False)
        for t in targets:
            logger.info(f"Monitoring {t.host} every {t.interval}s")

    def add_ping_job(
        self,
        host: str,
        interval: int = 60,
        rtt_warn_ms: Optional[float] = None,
        loss_warn_pct: Optional[float] = None,
    ):
        """Monitor *host* every *interval* seconds (replacing any earlier entry)."""
        self.add_ping_jobs(
            [
                HostConfig(
                    name=host,
                    interval=interval,
                    rtt_warn_ms=rtt_warn_ms,
                    loss_warn_pct=loss_warn_pct,
                )
            ]
        )

    def remove_ping_job(self, host: str):
        with self._lock:
            target = self._targets.pop(host, None)
            idle = self._release(target.interval) if target else None
        if idle is not None:
            idle.stop(wait=False)
        if target is not None:
            logger.info(f"Stopped monitoring {host}")

    def _start_sweep(self, interval: int) -> None:
        """Start the loop for *interval* (lock held, running)."""
        self._sweeps[interval] = sweep = _Sweep(self, interval)
        sweep.start()

    def _release(self, interval: int) -> Optional[_Sweep]:
        """Detach the loop for *interval* once no host uses it (lock held)."""
        if any(t.interval == interval for t in self._targets.values()):
            return None
        return self._sweeps.pop(interval, None)

    def list_jobs(self) -> List[dict]:
        """One entry per monitored host: ``host``, ``interval``, ``next_run_time``."""
        with self._lock:
            due = {i: s.next_run for i, s in self._sweeps.items()}
            targets = list(self._targets.values())
        when = {
            i: datetime.fromtimestamp(ts, timezone.utc).isoformat()
            for i, ts in due.items()
            if ts is not None
        }
        return [
            {
                "host": t.host,
                "interval": t.interval,
                "next_run_time": when.get(t.interval),
            }
            for t in targets
        ]

    def _sweep(self, interval: int) -> None:
        with self._lock:
            targets = [t for t in self._targets.values() if t.interval == interval]
        if not targets:
            return
        results = self.ping_service.ping_hosts([t.host for t in targets])
        with self.app.app_context():
            self.ping_service.save_ping_results(results)
            db.session.commit()
        logger.debug(
            "sweep every %ss: %d hosts, %d without reply",
            interval,
            len(results),
            sum(r["packet_loss"] for r in results),
        )
        changes = []
        for t, r in zip(targets, results):
            if t.alerter is not None:
                change = t.alerter.update(r["packet_loss"], r["response_time"])
                if change is not None:
                    changes.append((t.alerter, change))
        if changes:
            report(changes)
