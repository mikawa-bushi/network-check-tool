"""Always-on watcher: a light periodic loop that escalates to full diagnosis.

This is the two-tier model in one object:

* **Tier 1 (every tick, cheap):** run only a small, low-overhead probe set
  (reachability + DNS) so it's safe to run continuously, even on a busy network.
* **Tier 2 (on confirmed degradation, once):** the moment the debounced state
  machine flips to DEGRADED, run the *full* diagnosis a single time to capture
  the root cause, and attach that cause to the alert. We pay the heavier cost
  only when something is actually wrong — never on the steady-state loop.

Every tick's metrics are still recorded to storage so baselining and
change-point analysis keep working.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, List, Optional

from .alerting import AlertSink, FileAlertSink, StateMachine, Transition
from .diagnosis import Engine, runbook_steps
from .periodic import PeriodicLoop
from .storage import Storage

logger = logging.getLogger(__name__)

# Cheap, non-intrusive probes suitable for the steady-state loop. Heavy/active
# probes (http, tls, traceroute, peer throughput) are reserved for escalation or
# on-demand use.
LIGHT_PROBES = ["interface", "gateway", "external", "dns"]


class Watcher(PeriodicLoop):
    _tick_error_msg = "watcher tick failed"

    def __init__(
        self,
        config: Optional[dict] = None,
        interval: float = 30.0,
        light_probes: Optional[List[str]] = None,
        escalate: bool = True,
        sink: Optional[AlertSink] = None,
        storage: Optional[Storage] = None,
        fail_threshold: int = 2,
        recover_threshold: int = 2,
        save: bool = True,
        on_tick: Optional[Callable] = None,
        state_path=None,
        retention_raw_days: Optional[float] = 7,
        retention_rollup_days: float = 90,
        maintenance_interval: float = 3600.0,
    ) -> None:
        super().__init__()
        self.engine = Engine(config=config or {})
        self.interval = interval
        self.light_probes = light_probes or LIGHT_PROBES
        self.escalate = escalate
        self.sink = sink or FileAlertSink()
        self.storage = storage if storage is not None else (Storage() if save else None)
        self.save = save
        self.sm = StateMachine(
            fail_threshold=fail_threshold, recover_threshold=recover_threshold
        )
        self.on_tick = on_tick
        self.state_path = state_path
        self.retention_raw_days = retention_raw_days
        self.retention_rollup_days = retention_rollup_days
        self.maintenance_interval = maintenance_interval
        self._last_maintenance = time.time()

    # -- one observation ------------------------------------------------------
    def tick(self) -> Optional[Transition]:
        """Run one Tier-1 sweep; escalate + emit if the state changes."""
        target = self.engine.config.get("external_ip")
        diag = self.engine.run(only=self.light_probes)
        if self.storage is not None:
            self.storage.record_diagnosis(diag, target=target)

        primary = diag.primary
        transition = self.sm.observe(
            healthy=diag.healthy,
            cause=primary.cause if primary else None,
            confidence=primary.confidence if primary else None,
            summary=primary.summary if primary else "",
        )

        # On a *new* degradation, run the full diagnosis once for the real cause.
        if transition is not None and transition.event == "degraded" and self.escalate:
            self._escalate(transition, target)

        if transition is not None:
            self.sink.emit(transition)
        self._write_state()
        if self.on_tick:
            self.on_tick(diag, transition)
        return transition

    def _escalate(self, transition: Transition, target: Optional[str]) -> None:
        full = self.engine.run()  # full default sweep (still excludes traceroute)
        if self.storage is not None:
            self.storage.record_diagnosis(full, target=target)
        primary = full.primary
        if primary is not None:
            transition.cause = primary.cause
            transition.confidence = primary.confidence
            transition.summary = primary.summary
            transition.evidence = primary.evidence
            transition.consequences = primary.consequences
            transition.runbook = runbook_steps(primary.cause)

    def _write_state(self) -> None:
        if not self.state_path:
            return
        import json
        from pathlib import Path

        try:
            Path(self.state_path).write_text(
                json.dumps(self.sm.snapshot(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    # -- loop -----------------------------------------------------------------
    def _maybe_maintain(self) -> None:
        """Periodically downsample old data so a long-running daemon self-bounds disk."""
        if (
            self.storage is None
            or self.retention_raw_days is None
            or time.time() - self._last_maintenance < self.maintenance_interval
        ):
            return
        self._last_maintenance = time.time()
        try:
            self.storage.retention(
                raw_days=self.retention_raw_days, rollup_days=self.retention_rollup_days
            )
        except Exception:
            # Maintenance failure must not stop the watcher; record it.
            logger.warning("storage maintenance (retention) failed", exc_info=True)

    def _tick(self) -> None:
        self.tick()
        self._maybe_maintain()

    def _on_stop(self) -> None:
        if self.storage is not None:
            self.storage.close()
