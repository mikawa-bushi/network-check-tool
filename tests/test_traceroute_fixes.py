"""Path trace verdicts for names, IPv6 and a trace cut off by the time limit.

The tool output below is reconstructed from tracepath and Japanese Windows
tracert runs; the Windows sample is decoded from cp932 as the tool reads it.
"""

import pytest

from netdiag.core import Status
from netdiag.core.util import CommandResult, decode_output
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
TRACERT_CUT_OFF_JA = (
    "\r\n198.51.100.7 へのルートをトレースしています。経由するホップ数は最大 20 です\r\n\r\n"
    "  1    <1 ms    <1 ms    <1 ms  192.0.2.1\r\n"
    "  2     3 ms     2 ms     3 ms  192.0.2.254\r\n"
    "  3     *        *        *     要求がタイムアウトしました。\r\n"
    "  4     *        *        *     要求がタイムアウトしました。\r\n"
).encode("cp932")


def _trace(monkeypatch, target, output, windows=False, timed_out=False):
    monkeypatch.setattr(traceroute, "is_windows", lambda: windows)
    monkeypatch.setattr(traceroute, "have", lambda cmd: cmd in ("tracepath", "tracert"))
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
    output = decode_output(TRACERT_CUT_OFF_JA)
    res = _trace(monkeypatch, "198.51.100.7", output, windows=True, timed_out=True)
    assert (res.status, res.code) == (Status.UNKNOWN, "traceroute.timeout")
    assert "cut off after 60 s" in res.summary and "hop 2 (192.0.2.254)" in res.summary


def test_no_trace_tool_is_not_measured_rather_than_skipped(monkeypatch):
    monkeypatch.setattr(traceroute, "is_windows", lambda: False)
    monkeypatch.setattr(traceroute, "have", lambda cmd: False)
    diag = Engine(config={"trace_target": "192.0.2.1"}).run(only=["traceroute"])
    assert diag.results_by_name()["traceroute"].status == Status.UNKNOWN
    assert diag.status == Status.UNKNOWN
