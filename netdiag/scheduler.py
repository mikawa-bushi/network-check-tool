"""Lightweight periodic runner using stdlib threading only.

A single-process periodic diagnosis doesn't need APScheduler + a SQLAlchemy job
store. A ``threading.Timer``-style loop is enough and keeps the dependency list
empty. Each tick runs the engine and records every metric to storage so the
baseline and change-point features have data to work with.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, List, Optional

from .diagnosis import Engine
from .storage import Storage


class Monitor:
    def __init__(self, config: Optional[dict] = None, interval: float = 60.0,
                 storage: Optional[Storage] = None,
                 only: Optional[List[str]] = None,
                 on_tick: Optional[Callable] = None) -> None:
        self.engine = Engine(config=config or {})
        self.interval = interval
        self.storage = storage or Storage()
        self.only = only
        self.on_tick = on_tick
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            start = time.time()
            try:
                diag = self.engine.run(only=self.only)
                target = self.engine.config.get("external_ip")
                self.storage.record_diagnosis(diag, target=target)
                if self.on_tick:
                    self.on_tick(diag)
            except Exception:
                # A single failed tick must not kill the monitor loop.
                pass
            # Sleep the remainder of the interval, waking promptly on stop().
            elapsed = time.time() - start
            self._stop.wait(max(0.0, self.interval - elapsed))

    def start(self, background: bool = True) -> None:
        if background:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        else:
            self._loop()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
