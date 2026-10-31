"""netdiag.periodic.PeriodicLoop: the tick loop shared by every monitor."""

import threading
import time

from netdiag.periodic import PeriodicLoop


class _Counter(PeriodicLoop):
    def __init__(self, interval, fail=False, block=None):
        super().__init__()
        self.interval = interval
        self.fail = fail
        self.block = block
        self.ticks = 0
        self.ticked = threading.Event()

    def _tick(self):
        self.ticks += 1
        self.ticked.set()
        if self.block is not None:
            self.block.wait(5)
        if self.fail:
            raise RuntimeError("tick failed")


def test_ticks_right_away_and_survives_errors():
    loop = _Counter(interval=0.05, fail=True)
    loop.start()
    try:
        assert loop.ticked.wait(1)
        time.sleep(0.2)
    finally:
        loop.stop()
    assert loop.ticks >= 2  # a failing tick is logged, the loop goes on


def test_next_run_is_known_from_start_and_moves_an_interval_on():
    loop = _Counter(interval=60)
    loop.start()
    try:
        assert loop.next_run is not None  # set before the thread runs
        assert loop.ticked.wait(1)
        assert loop.next_run - time.time() > 50
    finally:
        loop.stop()


def test_stop_without_waiting_returns_during_a_tick():
    release = threading.Event()
    loop = _Counter(interval=60, block=release)
    loop.start()
    assert loop.ticked.wait(1)
    start = time.time()
    loop.stop(wait=False)
    assert time.time() - start < 0.5
    release.set()
    loop._thread.join(2)
    assert not loop._thread.is_alive() and loop.ticks == 1
