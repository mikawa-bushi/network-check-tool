"""Shared periodic-loop scaffolding for the stdlib-only daemons.

``Monitor`` and ``Watcher`` both run a tick on a fixed interval in a daemon
thread, swallow-and-log per-tick errors, and wake promptly on ``stop()``. That
machinery lives here so the two cannot drift apart; subclasses only implement
``_tick()`` (and may override ``_on_stop()`` for teardown).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional


class _PeriodicLoop:
    """Run ``_tick()`` every ``interval`` seconds on an optional daemon thread."""

    #: Default cadence; subclasses set ``self.interval`` in ``__init__``.
    interval: float = 60.0
    #: Logged (with traceback) when a tick raises; overridden per subclass.
    _tick_error_msg = "periodic tick failed"

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _tick(self) -> None:
        """Run one iteration. Subclasses implement this."""
        raise NotImplementedError

    def _on_stop(self) -> None:
        """Teardown after the loop ends (default: nothing)."""

    def _loop(self) -> None:
        # Log under the subclass's own module so callers can filter by it
        # (e.g. "netdiag.scheduler" vs "netdiag.watch").
        log = logging.getLogger(type(self).__module__)
        while not self._stop.is_set():
            start = time.time()
            try:
                self._tick()
            except Exception:
                # One failed tick must never kill the loop, but log it so the
                # failure is observable instead of silently dropped.
                log.warning(self._tick_error_msg, exc_info=True)
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
        self._on_stop()
