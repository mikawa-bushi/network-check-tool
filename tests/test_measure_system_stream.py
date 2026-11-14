"""Streaming through the OS ``ping`` (soaks of IPv6 devices, or of IPv4 ones
on a host without unprivileged ICMP sockets).

A scripted stand-in for ``ping`` plays back reconstructed iputils, BSD and
Japanese ping.exe output line by line, with the timing a real ping has.
"""

import json
import sys
import threading
import time

import pytest

from netdiag.core import util
from netdiag.core.util import CommandResult
from netdiag.measure import icmp as icmpmod

HOST = "192.0.2.10"

FAKE_PING = r"""
import json, sys, time
plan, counter = sys.argv[1], sys.argv[2]
with open(plan, encoding="utf-8") as f:
    runs = json.load(f)
try:
    with open(counter) as f:
        n = int(f.read())
except OSError:
    n = 0
with open(counter, "w") as f:
    f.write(str(n + 1))
run = runs[min(n, len(runs) - 1)]
out = sys.stdout.buffer
start, due = time.monotonic(), 0.0
for delay, text in run["steps"]:
    due += delay  # on ping's own clock: one late line does not delay the rest
    time.sleep(max(0.0, start + due - time.monotonic()))
    out.write((text + run.get("newline", "\n")).encode(run.get("encoding", "utf-8")))
    out.flush()
open(counter + ".exited", "w").close()
sys.exit(run.get("exit", 0))
"""


@pytest.fixture
def fake_ping(tmp_path, monkeypatch):
    """Make every ping run play back the next of the given runs (the last repeats).

    Returns the path of a file that appears once a run has exited.
    """
    script = tmp_path / "fake_ping.py"
    script.write_text(FAKE_PING, encoding="utf-8")
    plan = tmp_path / "plan.json"
    argv = [sys.executable, str(script), str(plan), str(tmp_path / "runs")]
    monkeypatch.setenv("NETDIAG_PING_METHOD", "system")
    monkeypatch.setattr(icmpmod, "have", lambda cmd: True)
    monkeypatch.setattr(icmpmod, "is_macos", lambda: False)
    monkeypatch.setattr(icmpmod, "_RUN_SECONDS", 1.0, raising=False)
    monkeypatch.setattr(icmpmod, "build_ping_args", lambda *a, **k: argv)

    def play(*runs, windows=False):
        monkeypatch.setattr(icmpmod, "is_windows", lambda: windows)
        plan.write_text(json.dumps(list(runs)), encoding="utf-8")
        return tmp_path / "runs.exited"

    return play


def iputils(count, lost=(), transmitted=None, header=None, base=1):
    """A reconstructed iputils run: one reply every 0.2 s except the *lost* ones."""
    steps = [[0.0, header or f"PING {HOST} ({HOST}) 56(84) bytes of data."]]
    wait = 0.0
    for i in range(count):
        wait += 0.2 if i else 0.0
        if i not in lost:
            line = (
                f"64 bytes from {HOST}: icmp_seq={base + i} ttl=64 time=0.{40 + i} ms"
            )
            steps.append([wait, line])
            wait = 0.0
    sent = count if transmitted is None else transmitted
    got = count - len(lost)
    steps += [
        [wait + 0.05, ""],
        [0.0, f"--- {HOST} ping statistics ---"],
        [0.0, f"{sent} packets transmitted, {got} received, time 800ms"],
    ]
    return {"steps": steps}


NO_ROUTE = {"steps": [[0.0, "ping: connect: Network is unreachable"]], "exit": 2}


def stream(seconds, timeout=0.5, until=None):
    """Run ping_stream for *seconds*, or until *until()* is true; return its
    samples, its result and when it was stopped."""
    samples, result = [], {}
    stop = threading.Event()

    def on_sample(target, sent_at, rtt, error):
        samples.append((sent_at, rtt, error, time.time()))

    th = threading.Thread(
        target=lambda: result.update(
            icmpmod.ping_stream([HOST], on_sample, stop, interval=0.2, timeout=timeout)
        )
    )
    th.start()
    deadline = time.monotonic() + seconds
    while th.is_alive() and time.monotonic() < deadline:
        if until is not None and until():
            break
        time.sleep(0.01)
    stopped = time.time()
    stop.set()
    th.join(10)
    assert not th.is_alive()
    return samples, result[HOST], stopped


def test_a_ping_run_that_cannot_send_counts_as_lost_and_the_stream_goes_on(
    fake_ping,
):
    fake_ping(iputils(5), NO_ROUTE, iputils(5))
    samples, total, _ = stream(2.5)
    lost = [s for s in samples if s[1] is None]
    assert len(lost) >= 2
    assert {s[2] for s in lost} == {"connect: Network is unreachable"}
    assert max(s[0] for s in samples if s[1] is not None) > max(s[0] for s in lost)
    assert total.error is None
    assert total.errors["connect: Network is unreachable"] == len(lost)


def test_each_reply_arrives_as_ping_prints_it(fake_ping):
    fake_ping(iputils(5))
    samples, _total, _ = stream(1.5)
    assert len(samples) >= 5
    assert max(arrived - sent_at for sent_at, _r, _e, arrived in samples) < 0.3


def test_a_lost_first_request_is_placed_first_when_seq_counts_from_0(fake_ping):
    bsd = f"PING {HOST} ({HOST}): 56 data bytes"
    fake_ping(iputils(5, lost={0}, header=bsd, base=0))
    samples, _total, _ = stream(1.0)
    first = min(samples)
    assert first[1] is None  # was placed last: [r, r, r, r, lost]
    assert sorted(s[1] is None for s in samples[:5]).count(True) == 1


def test_ping_cut_short_by_ctrl_c_does_not_count_unsent_requests_as_lost(
    fake_ping,
):
    # Ctrl-C reaches ping too: it prints its statistics after 3 of 5 and exits,
    # and netdiag's stop follows at once, however slowly ping started.
    exited = fake_ping(iputils(3, transmitted=3))
    samples, total, stopped = stream(5.0, until=exited.exists)
    assert [s[1] is None for s in samples] == [False, False, False]
    assert total.sent == total.received == 3
    assert all(sent_at <= stopped for sent_at, *_ in samples)


def test_japanese_ping_exe_lines_stream_in_order_as_they_print(fake_ping, monkeypatch):
    # Reconstructed Japanese Windows ping.exe output, cp932 with CRLF, read as
    # on a Japanese Windows PC, whose console code page is 932.
    monkeypatch.setattr(util, "_fallback_encodings", lambda: ["cp932"])
    lines = [
        "",
        f"{HOST} に ping を送信しています 32 バイトのデータ:",
        f"{HOST} からの応答: バイト数 =32 時間 <1ms TTL=64",
        "要求がタイムアウトしました。",
        f"{HOST} からの応答: バイト数 =32 時間 =3ms TTL=64",
        "",
        f"{HOST} の ping 統計:",
        "    パケット数: 送信 = 3、受信 = 2、損失 = 1 (33% の損失)、",
    ]
    delays = [0.0, 0.0, 0.05, 0.3, 0.05, 0.05, 0.0, 0.0]
    exited = fake_ping(
        {
            "steps": [list(s) for s in zip(delays, lines)],
            "encoding": "cp932",
            "newline": "\r\n",
        },
        windows=True,
    )
    samples, _total, _ = stream(5.0, until=exited.exists)
    assert [s[1] for s in samples[:3]] == [0.5, None, 3.0]
    assert samples[1][2] == "要求がタイムアウトしました。"
    assert all(0 <= arrived - sent_at < 0.6 for sent_at, _r, _e, arrived in samples)


def test_stream_ends_early_with_the_reason_when_ping_cannot_run(fake_ping, monkeypatch):
    fake_ping(iputils(5))
    monkeypatch.setattr(
        icmpmod, "build_ping_args", lambda *a, **k: ["/nonexistent/netdiag-ping"]
    )
    start = time.monotonic()
    samples, total, _ = stream(5.0, timeout=0.3)
    assert time.monotonic() - start < 2.0  # returned by itself, before stop
    assert samples == []
    assert "command not available" in total.error


def test_batch_ping_without_a_route_is_all_lost_not_cannot_measure(monkeypatch):
    monkeypatch.setattr(icmpmod, "is_windows", lambda: False)
    monkeypatch.setattr(icmpmod, "have", lambda cmd: True)
    no_route = CommandResult(
        False, 2, "", "ping: connect: Network is unreachable\n", True
    )
    monkeypatch.setattr(icmpmod, "run", lambda args, timeout: no_route)
    st = icmpmod.system_ping(HOST, count=4)
    assert st.error is None  # the PC cannot reach it: lost, as on the socket back-end
    assert (st.sent, st.received) == (4, 0)
    assert st.errors == {"connect: Network is unreachable": 4}
