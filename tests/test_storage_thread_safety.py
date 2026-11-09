"""Tests for issue #12 (M4): Storage thread safety.

`Storage` opened a single sqlite connection on the main thread, but
`Watcher.start()` defaults to ``background=True`` and writes from a worker
thread -- which raised ``sqlite3.ProgrammingError`` (swallowed by the loop, so
nothing was recorded). These tests pin safe cross-thread use.
"""

import threading
import time
from types import SimpleNamespace

from netdiag.storage import Storage
from netdiag.watch import Watcher


def _fake_result(value=1.0):
    return SimpleNamespace(
        name="external",
        status=SimpleNamespace(value="ok"),
        metrics={"rtt_ms": value},
        summary="ok",
        timestamp=time.time(),
    )


class _FakeDiag:
    def __init__(self):
        self.results = [_fake_result()]
        self.primary = None
        self.healthy = True


class _FakeEngine:
    def __init__(self):
        self.config = {}

    def run(self, only=None):
        return _FakeDiag()


class _NullSink:
    def emit(self, transition):
        pass


# ----------------------------------------------------------- storage internals


def test_record_from_another_thread(tmp_path):
    st = Storage(tmp_path / "t.db")
    errors = []

    def worker():
        try:
            st.record_result(_fake_result(), target="1.1.1.1")
        except Exception as e:  # pragma: no cover - only on regression
            errors.append(e)

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert errors == [], errors
    assert st.counts()["samples"] == 1
    st.close()


def test_concurrent_writers_are_serialized(tmp_path):
    st = Storage(tmp_path / "t.db")
    errors = []

    def worker():
        try:
            for _ in range(20):
                st.record_result(_fake_result(), target="1.1.1.1")
        except Exception as e:  # pragma: no cover - only on regression
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], errors
    assert st.counts()["samples"] == 100
    st.close()


# --------------------------------------------------------- public start() APIs


def _wait_for_samples(st, deadline=3.0):
    end = time.time() + deadline
    while time.time() < end:
        if st.counts()["samples"] > 0:
            return True
        time.sleep(0.05)
    return False


def test_watcher_background_records(tmp_path):
    st = Storage(tmp_path / "w.db")
    watcher = Watcher(
        interval=0.05,
        storage=st,
        sink=_NullSink(),
        escalate=False,
    )
    watcher.engine = _FakeEngine()
    watcher.start()  # background=True by default
    try:
        assert _wait_for_samples(st)
    finally:
        watcher.stop()  # closes storage
