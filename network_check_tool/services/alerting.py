"""Per-host threshold alerting for the ping monitor (F3).

Wraps netdiag's flap-damping ``StateMachine``: each ping sample is classified
healthy/unhealthy (packet loss, or RTT over ``rtt_warn_ms``) and a *confirmed*
DEGRADED/RECOVERED transition is emitted to the configured alert sink. State is
in-memory per process and resets on restart -- persisting it across restarts is a
deliberate later enhancement.
"""

from __future__ import annotations

import logging
from typing import Optional

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
        engine_config: Optional[dict] = None,
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
        self.engine_config = engine_config or {}
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
        """Feed one ping sample; emit (and return) a Transition iff state changed."""
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
        transition = self._sm.observe(healthy, cause=self.host, summary=reason)
        if transition is not None:
            if self.diagnose and transition.event == "degraded":
                self._attach_diagnosis(transition)
            try:
                self.sink.emit(transition)
            except Exception:
                # A failing sink must never break the monitoring loop.
                logger.warning("alert sink failed for %s", self.host, exc_info=True)
        return transition

    def _attach_diagnosis(self, transition: Transition) -> None:
        """Diagnose *this* host once and fold the root cause into the alert.

        Triggered only on a confirmed DEGRADED transition, so the state machine
        rate-limits it to once per outage episode. netdiag first checks this
        PC's own network (a broken local link explains every host at once), then
        the host itself: route/subnet, ARP presence, ping statistics. Any failure
        here is logged, never propagated.
        """
        try:
            from netdiag.diagnosis import runbook_steps
            from netdiag.runner import diagnose_target

            primary = diagnose_target(self.host, config=self.engine_config)
            if primary is not None:
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
            else:
                transition.evidence = list(transition.evidence) + [
                    "netdiag: no local network fault found"
                ]
        except Exception:
            logger.warning("auto-netdiag failed for %s", self.host, exc_info=True)


class DbAlertSink:
    """Keep alerts in the dashboard database (the line board shows the latest).

    Runs in the scheduler's worker threads, so it pushes its own app context.
    """

    def __init__(self, app) -> None:
        self.app = app

    def emit(self, transition: Transition) -> None:
        from ..models import AlertEvent, db

        with self.app.app_context():
            db.session.add(AlertEvent.from_transition(transition))
            db.session.commit()
