"""Tests for issue #14 (E1): observable daemon error handling.

The daemon loops and fan-out sinks deliberately swallow exceptions so one
failure can't kill a long-running process -- but they did so silently. These
tests pin that failures are now logged (with a traceback) while the swallow
behaviour is preserved, and that the library stays quiet until the CLI opts in.
"""

import logging
import time

import pytest

from netdiag.alerting import MultiSink
from netdiag.storage import Storage


@pytest.fixture(autouse=True)
def _restore_netdiag_logger():
    """Isolate tests that mutate the process-global ``netdiag`` logger."""
    lg = logging.getLogger("netdiag")
    handlers = lg.handlers[:]
    level = lg.level
    propagate = lg.propagate
    try:
        yield
    finally:
        # Close any handler the test added before restoring, so FileHandlers
        # don't leak their file descriptor past the test.
        for h in lg.handlers[:]:
            if h not in handlers:
                h.close()
        lg.handlers[:] = handlers
        lg.setLevel(level)
        lg.propagate = propagate


class _FailingSink:
    def emit(self, transition):
        raise RuntimeError("sink boom")


class _RecordingSink:
    def __init__(self):
        self.events = []

    def emit(self, transition):
        self.events.append(transition)


class _BoomEngine:
    def __init__(self):
        self.config = {}

    def run(self, only=None):
        raise RuntimeError("engine boom")


class _NullSink:
    def emit(self, transition):
        pass


def test_multisink_logs_failure_and_continues(caplog):
    recording = _RecordingSink()
    sink = MultiSink([_FailingSink(), recording])
    sentinel = object()

    with caplog.at_level(logging.WARNING, logger="netdiag"):
        sink.emit(sentinel)  # must not raise

    # The healthy sink still received the event despite the failing one.
    assert recording.events == [sentinel]
    # The failure was logged, not swallowed silently.
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "expected a warning log for the failing sink"
    assert any(r.exc_info for r in warnings), "expected a traceback (exc_info)"


def test_watcher_loop_logs_tick_failure(tmp_path, caplog):
    from netdiag.watch import Watcher

    st = Storage(tmp_path / "w.db")
    watcher = Watcher(interval=0.05, storage=st, sink=_NullSink(), escalate=False)
    watcher.engine = _BoomEngine()

    with caplog.at_level(logging.WARNING, logger="netdiag"):
        watcher.start()  # background
        try:
            deadline = time.time() + 3
            got = False
            while time.time() < deadline:
                if any(
                    r.name.startswith("netdiag.watch") and r.levelno >= logging.WARNING
                    for r in caplog.records
                ):
                    got = True
                    break
                time.sleep(0.05)
        finally:
            watcher.stop()  # closes storage

    assert got, "expected a warning log from the watcher loop on tick failure"


def test_library_is_quiet_by_default():
    lg = logging.getLogger("netdiag")
    assert any(
        isinstance(h, logging.NullHandler) for h in lg.handlers
    ), "netdiag logger should carry a NullHandler so it is silent by default"
    assert not any(
        isinstance(h, (logging.StreamHandler, logging.FileHandler))
        and not isinstance(h, logging.NullHandler)
        for h in lg.handlers
    ), "library must not attach a real handler on its own"


def test_setup_logging_verbose_attaches_stream_handler():
    from netdiag.cli import _setup_logging

    _setup_logging(verbose=True)
    lg = logging.getLogger("netdiag")
    assert lg.level == logging.DEBUG
    assert any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.NullHandler)
        for h in lg.handlers
    )


def test_setup_logging_writes_to_file(tmp_path):
    from netdiag.cli import _setup_logging

    log_path = tmp_path / "netdiag.log"
    _setup_logging(log_file=str(log_path))
    logging.getLogger("netdiag.test").warning("hello-from-test")
    for h in logging.getLogger("netdiag").handlers:
        h.flush()

    assert log_path.exists()
    assert "hello-from-test" in log_path.read_text(encoding="utf-8")
