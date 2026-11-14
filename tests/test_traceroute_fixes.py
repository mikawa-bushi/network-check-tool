"""Path trace verdicts for names, IPv6 and a trace cut off by the time limit.

The tool output below is reconstructed from tracepath and traceroute runs.
"""

import pytest

from netdiag.core import Status
from netdiag.core.util import CommandResult
from netdiag.diagnosis import Engine
from netdiag.probes import traceroute

TRACEPATH_LOCALHOST = (
    " 1?: [LOCALHOST]                      pmtu 65535\n"
    " 1:  127.0.0.1                                             0.052ms reached\n"
    "     Resume: pmtu 65535 hops 1 back 1\n"
)
TRACEPATH_V6 = (
    " 1?: [LOCALHOST]                        0.010ms pmtu 65536\n"
    " 1:  ::1                                                   0.040ms reached\n"
    "     Resume: pmtu 65536 hops 1 back 1\n"
)
TRACEROUTE_CUT_OFF = (
    "traceroute to 198.51.100.7 (198.51.100.7), 20 hops max, 60 byte packets\n"
    " 1  192.0.2.1  0.412 ms\n"
    " 2  192.0.2.254  2.903 ms\n"
    " 3  *\n"
    " 4  *\n"
)


def _trace(monkeypatch, target, output, timed_out=False):
    monkeypatch.setattr(traceroute, "have", lambda cmd: cmd == "tracepath")
    res = CommandResult(
        ok=not timed_out, code=0, stdout=output, stderr="", found=True,
        timed_out=timed_out,
    )  # fmt: skip
    monkeypatch.setattr(traceroute, "run", lambda *a, **k: res)
    diag = Engine(config={"trace_target": target}).run(only=["traceroute"])
    return diag.results_by_name()["traceroute"]


@pytest.mark.parametrize(
    "target, output",
    [("localhost", TRACEPATH_LOCALHOST), ("::1", TRACEPATH_V6)],
)
def test_a_name_or_ipv6_target_that_was_reached_is_ok(monkeypatch, target, output):
    res = _trace(monkeypatch, target, output)
    assert (res.status, res.code) == (Status.OK, "traceroute.ok")


def test_a_trace_cut_off_by_the_time_limit_says_so(monkeypatch):
    res = _trace(monkeypatch, "198.51.100.7", TRACEROUTE_CUT_OFF, timed_out=True)
    assert (res.status, res.code) == (Status.UNKNOWN, "traceroute.timeout")
    assert "cut off after 60 s" in res.summary and "hop 2 (192.0.2.254)" in res.summary


def test_no_trace_tool_is_not_measured_rather_than_skipped(monkeypatch):
    monkeypatch.setattr(traceroute, "have", lambda cmd: False)
    diag = Engine(config={"trace_target": "192.0.2.1"}).run(only=["traceroute"])
    assert diag.results_by_name()["traceroute"].status == Status.UNKNOWN
    assert diag.status == Status.UNKNOWN
