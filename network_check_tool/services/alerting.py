"""Per-host threshold alerting for the ping monitor (F3).

Wraps netdiag's flap-damping ``StateMachine``: each ping sample is classified
healthy/unhealthy (packet loss, or RTT over ``rtt_warn_ms``) and a *confirmed*
DEGRADED/RECOVERED transition is emitted to the configured alert sink. State is
in-memory per process and resets on restart -- persisting it across restarts is a
deliberate later enhancement.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from netdiag.alerting import StateMachine, Transition

logger = logging.getLogger(__name__)

# Consecutive bad samples before a host is DEGRADED (the line board's "down"
# uses the same rule, so tiles and alerts agree).
FAIL_THRESHOLD = 2


class HostAlerter:
    """Classify ping samples for one host and emit state-change alerts."""

    def __init__(
        self,
        host: str,
        sink,
        rtt_warn_ms: Optional[float] = None,
        loss_warn_pct: Optional[float] = None,
        fail_threshold: int = FAIL_THRESHOLD,
        recover_threshold: int = 2,
        diagnose: bool = False,
    ) -> None:
        self.host = host
        self.sink = sink
        self.rtt_warn_ms = rtt_warn_ms
        # Reserved for a future windowed loss% check; a single ping sample only
        # exposes a boolean loss, so per-sample classification uses packet_loss.
        self.loss_warn_pct = loss_warn_pct
        # F1: when set, diagnose this host with netdiag on a confirmed
        # degradation and attach the root cause to the alert.
        self.diagnose = diagnose
        self._sm = StateMachine(
            fail_threshold=fail_threshold,
            recover_threshold=recover_threshold,
            host=host,
        )

    def _is_healthy(self, packet_loss: bool, response_time: Optional[float]) -> bool:
        if packet_loss:
            return False
        if self.rtt_warn_ms is not None and response_time is not None:
            return response_time <= self.rtt_warn_ms
        return True

    def observe(
        self, packet_loss: bool, response_time: Optional[float]
    ) -> Optional[Transition]:
        """Feed one sample; emit (and return) a Transition iff state changed."""
        transition = self.update(packet_loss, response_time)
        if transition is not None:
            report([(self, transition)])
        return transition

    def update(
        self, packet_loss: bool, response_time: Optional[float]
    ) -> Optional[Transition]:
        """Feed one sample; return the confirmed state change without emitting it."""
        from netdiag.i18n import tr

        healthy = self._is_healthy(packet_loss, response_time)
        if packet_loss:
            reason = tr(f"{self.host}: 応答なし", f"{self.host}: packet loss")
        elif not healthy:
            reason = tr(
                f"{self.host}: 応答 {response_time}ms（しきい値 {self.rtt_warn_ms}ms 超）",
                f"{self.host}: rtt {response_time}ms over {self.rtt_warn_ms}ms",
            )
        else:
            reason = tr(
                f"{self.host}: 応答 {response_time}ms",
                f"{self.host}: rtt {response_time}ms",
            )
        return self._sm.observe(healthy, cause=self.host, summary=reason)

    def explain(self, transition: Transition, primary) -> None:
        """Fold netdiag's root cause for this host (None: none found) into the alert."""
        if primary is None:
            transition.evidence = list(transition.evidence) + [
                "netdiag: no local network fault found"
            ]
            return
        from netdiag.diagnosis import runbook_steps

        transition.cause = primary.cause
        transition.confidence = primary.confidence
        transition.diagnosis = str(primary.summary)
        transition.evidence = list(transition.evidence) + [
            f"netdiag root cause: {primary.cause} — {primary.summary}"
        ]
        transition.runbook = list(
            getattr(primary, "runbook", None) or runbook_steps(primary.cause)
        )
        logger.warning(
            "auto-netdiag for %s: root cause = %s (%s)",
            self.host,
            primary.cause,
            primary.confidence,
        )

    def emit(self, transition: Transition) -> None:
        try:
            self.sink.emit(transition)
        except Exception:
            # A failing sink must never break the monitoring loop.
            logger.warning("alert sink failed for %s", self.host, exc_info=True)


def report(changes: List[Tuple[HostAlerter, Transition]]) -> None:
    """Emit confirmed state changes, explaining new outages first.

    Every host that just went down (and asked for a diagnosis) is diagnosed in
    one netdiag run: it checks this PC's own network once (a broken local link
    explains every device at once), then the devices side by side -- route,
    ARP presence, ping statistics. Triggered only on a confirmed DEGRADED
    transition, so it runs at most once per outage episode. A failed diagnosis
    is logged, never propagated; the alerts still go out.
    """
    down = [(a, t) for a, t in changes if a.diagnose and t.event == "degraded"]
    if down:
        try:
            from netdiag.runner import diagnose_targets

            causes = diagnose_targets([a.host for a, _ in down])
        except Exception:
            logger.warning(
                "auto-netdiag failed for %s",
                ", ".join(a.host for a, _ in down),
                exc_info=True,
            )
        else:
            for a, t in down:
                a.explain(t, causes.get(a.host))
    for a, t in changes:
        a.emit(t)


class DbAlertSink:
    """Keep alerts in the dashboard database (the line board shows the latest).

    Runs on the monitor's sweep threads, so it pushes its own app context.
    """

    def __init__(self, app) -> None:
        self.app = app

    def emit(self, transition: Transition) -> None:
        from ..models import AlertEvent, db

        with self.app.app_context():
            db.session.add(AlertEvent.from_transition(transition))
            db.session.commit()
