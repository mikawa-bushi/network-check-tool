"""Tests for the debounced state machine and the file alert sink.

The state machine is pure logic (no network), so we can assert its flap-damping
behaviour precisely: how many consecutive bad/good observations are needed before
an event fires, and that blips below the threshold stay silent.
"""

import json

from netdiag.alerting import FileAlertSink, State, StateMachine, Transition


def _feed(sm, observations):
    """Feed a list of (healthy: bool) and collect emitted transitions."""
    out = []
    for i, healthy in enumerate(observations):
        t = sm.observe(healthy=healthy, ts=float(i), cause=None if healthy else "dns")
        if t:
            out.append(t)
    return out


def test_single_blip_does_not_alert():
    sm = StateMachine(fail_threshold=2, recover_threshold=2)
    # One bad tick surrounded by good ones must not fire (debounce).
    events = _feed(sm, [True, True, False, True, True])
    assert events == []
    assert sm.state == State.HEALTHY or sm.state == State.UNKNOWN


def test_confirmed_degradation_fires_once():
    sm = StateMachine(fail_threshold=2, recover_threshold=2)
    events = _feed(sm, [True, False, False, False])
    assert len(events) == 1
    assert events[0].event == "degraded"
    assert events[0].cause == "dns"
    assert sm.state == State.DEGRADED


def test_degraded_then_recovered():
    sm = StateMachine(fail_threshold=2, recover_threshold=2)
    events = _feed(sm, [False, False, True, True])
    kinds = [e.event for e in events]
    assert kinds == ["degraded", "recovered"]
    rec = events[1]
    assert rec.duration_s is not None and rec.duration_s >= 0
    assert sm.state == State.HEALTHY


def test_starts_broken_emits_degraded_without_spurious_recovered():
    # If the very first observations are bad, we should get DEGRADED and never a
    # phantom RECOVERED from the initial UNKNOWN state.
    sm = StateMachine(fail_threshold=1, recover_threshold=1)
    events = _feed(sm, [False])
    assert [e.event for e in events] == ["degraded"]
    assert events[0].from_state == State.UNKNOWN.value
    assert events[0].duration_s is None  # no prior committed state to time


def test_flapping_emits_bounded_events():
    sm = StateMachine(fail_threshold=2, recover_threshold=2)
    # Rapid alternation should NOT cross either threshold -> no events.
    events = _feed(sm, [False, True, False, True, False, True])
    assert events == []


def test_file_sink_writes_jsonl_and_log(tmp_path):
    jsonl = tmp_path / "alerts.jsonl"
    log = tmp_path / "alerts.log"
    sink = FileAlertSink(jsonl_path=jsonl, log_path=log)
    t = Transition(
        event="degraded",
        from_state="healthy",
        to_state="degraded",
        ts=1_000_000.0,
        cause="dns",
        confidence="high",
        summary="resolver down",
        consequences=["http"],
    )
    sink.emit(t)

    lines = jsonl.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event"] == "degraded"
    assert rec["cause"] == "dns"
    assert "iso" in rec
    assert "DEGRADED" in log.read_text()
    assert "dns" in log.read_text()
