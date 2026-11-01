"""Lightweight periodic runner using stdlib threading only.

A single-process periodic diagnosis doesn't need APScheduler + a SQLAlchemy job
store. A ``threading.Timer``-style loop is enough and keeps the dependency list
empty. Each tick runs the engine and records every metric to storage so the
baseline and change-point features have data to work with.
"""

from __future__ import annotations

from typing import Callable, List, Optional

from .diagnosis import Engine
from .periodic import PeriodicLoop
from .storage import Storage


class Monitor(PeriodicLoop):
    _tick_error_msg = "monitor tick failed"

    def __init__(
        self,
        config: Optional[dict] = None,
        interval: float = 60.0,
        storage: Optional[Storage] = None,
        only: Optional[List[str]] = None,
        on_tick: Optional[Callable] = None,
    ) -> None:
        super().__init__()
        self.engine = Engine(config=config or {})
        self.interval = interval
        self.storage = storage or Storage()
        self.only = only
        self.on_tick = on_tick

    def _tick(self) -> None:
        diag = self.engine.run(only=self.only)
        target = self.engine.config.get("external_ip")
        self.storage.record_diagnosis(diag, target=target)
        if self.on_tick:
            self.on_tick(diag)
