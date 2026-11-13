"""Regression tests for the measurement layer: statistics, DNS, TCP and ping."""

import _thread
import socket
import struct
import threading
import time

import pytest

from netdiag.core.util import CommandResult
from netdiag.measure import icmp as icmpmod
from netdiag.measure import tcp as tcpmod
from netdiag.measure import winicmp
from netdiag.measure.dns import DnsReplyError, query
from netdiag.measure.stats import summarize
from netdiag.measure.tcp import tcp_check, tcp_connect_once


def test_p95_of_zero_rtts_is_zero_not_the_first_sample():
    assert summarize([7.0] + [0.0] * 99).p95 == 0.0


# ------------------------------------------------------------------ DNS replies


def _serve_once(replies):
    """A one-shot UDP resolver on localhost that answers with *replies(query)*."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))

    def serve():
        data, addr = srv.recvfrom(512)
        for reply in replies(data):
            srv.sendto(reply, addr)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return srv, srv.getsockname()[1]


def _header(query_bytes, answers):
    return query_bytes[:2] + struct.pack("!HHHHH", 0x8180, 1, answers, 0, 0)


def _a_record(i):
    return (
        b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + bytes([198, 51, 100, i % 256])
    )


def test_dns_reply_cut_inside_a_record_raises_dns_reply_error():
    def cut(q):
        yield _header(q, 1) + q[12:] + b"\xc0\x0c" + struct.pack("!HH", 1, 1)

    srv, port = _serve_once(cut)
    with srv, pytest.raises(DnsReplyError):
        query("plc.example", "127.0.0.1", timeout=1.0, port=port)


def test_dns_reply_larger_than_4096_bytes_is_read_whole():
    def big(q):
        yield _header(q, 300) + q[12:] + b"".join(_a_record(i) for i in range(300))

    srv, port = _serve_once(big)
    with srv:
        ans = query("plc.example", "127.0.0.1", timeout=1.0, port=port)
    assert ans.ok and len(ans.addresses) == 300


def test_dns_stray_datagram_with_another_id_is_skipped():
    def stray_then_ours(q):
        other = bytes([q[0] ^ 0xFF, q[1]])
        yield other + _header(q, 0)[2:] + q[12:]
        yield _header(q, 1) + q[12:] + _a_record(7)

    srv, port = _serve_once(stray_then_ours)
    with srv:
        ans = query("plc.example", "127.0.0.1", timeout=1.0, port=port)
    assert ans.addresses == ["198.51.100.7"]


# ------------------------------------------------------------------ TCP connect


@pytest.fixture
def listener():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    with srv:
        yield srv.getsockname()[1]


def _slow_name_lookup(monkeypatch, delay):
    real = socket.getaddrinfo
    lookups = []

    def fake(host, port, *args, **kw):
        numeric_only = kw.get("flags", 0) & socket.AI_NUMERICHOST
        if host == "plc01.example" and not numeric_only:
            lookups.append(host)
            time.sleep(delay)
            host = "127.0.0.1"
        return real(host, port, *args, **kw)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return lookups


def test_tcp_connect_time_excludes_a_slow_name_lookup(monkeypatch, listener):
    lookups = _slow_name_lookup(monkeypatch, 0.4)
    res = tcp_check("plc01.example", listener, attempts=3, timeout=0.5, pause=0)
    assert res.state == "open"
    assert max(res.times_ms) < 200  # the 400 ms lookup is not connect time
    assert lookups == ["plc01.example"]  # looked up once, not per attempt


def test_tcp_check_bounds_a_hung_name_lookup(monkeypatch, listener):
    _slow_name_lookup(monkeypatch, 5.0)
    monkeypatch.setattr(tcpmod, "LOOKUP_TIMEOUT", 0.2)
    start = time.monotonic()
    res = tcp_check("plc01.example", listener, attempts=2, timeout=0.3)
    assert time.monotonic() - start < 1.0
    assert (res.state, res.reasons) == ("error", {"name resolution failed": 1})


def test_tcp_port_above_65535_is_rejected_not_wrapped(listener):
    res = tcp_check("127.0.0.1", listener + 65536, attempts=1, timeout=0.5)
    assert res.state == "error"
    assert list(res.reasons) == [f"port {listener + 65536} out of range (1-65535)"]
    ok, _ms, reason = tcp_connect_once("127.0.0.1", listener + 65536)
    assert not ok and "out of range" in reason


# ------------------------------------------------------------- pace after a stall


def _densest(times, window):
    """Most send times that fall inside any *window* seconds."""
    times = sorted(times)
    return max(sum(1 for x in times if s <= x < s + window) for s in times)


def _stall_once(seconds):
    """An on_sample that freezes the pinger thread once, like Ctrl-Z then fg."""
    seen = []

    def on_sample(target, sent_at, rtt, error):
        seen.append(sent_at)
        if len(seen) == 3:
            time.sleep(seconds)

    return seen, on_sample


@pytest.mark.skipif(
    not icmpmod.icmp_socket_available(), reason="no unprivileged ICMP socket"
)
def test_socket_pinger_resumes_its_pace_after_a_stall():
    sent, on_sample = _stall_once(1.0)
    stop = threading.Event()
    threading.Timer(2.0, stop.set).start()
    icmpmod.ping_stream(
        ["127.0.0.1"], on_sample, stop, interval=0.05, timeout=0.5, method="socket"
    )
    assert len(sent) >= 15
    assert _densest(sent, 0.03) <= 2  # was 20 requests at once after the stall


class _EchoApi:
    """Windows ICMP API stand-in: every echo answers at once."""

    def __init__(self):
        self.calls = []

    def open(self):
        return 1

    def close(self, handle):
        pass

    def echo(self, handle, ip, payload, timeout_ms):
        self.calls.append(time.perf_counter())
        return winicmp.IP_SUCCESS, 1, 0


def test_windows_pinger_resumes_its_pace_after_a_stall():
    api = _EchoApi()
    _sent, on_sample = _stall_once(0.5)
    stop = threading.Event()
    threading.Timer(1.2, stop.set).start()
    winicmp.WinIcmpPinger(
        {"plc": "192.0.2.10"},
        interval=0.05,
        timeout=0.2,
        size=32,
        max_pps=1000,
        on_sample=on_sample,
        stop=stop,
        api=api,
    ).run()
    assert len(api.calls) >= 10
    assert _densest(api.calls, 0.03) <= 2  # was 10 echoes back to back


# ------------------------------------------------------------- Ctrl-C in a sweep


def _ctrl_c_after(seconds):
    # interrupt_main raises KeyboardInterrupt in the main thread but, like
    # Ctrl-C on Windows, cannot break an untimed wait it is blocked in.
    threading.Timer(seconds, _thread.interrupt_main).start()


def _interrupted_sweep(**kw):
    _ctrl_c_after(0.3)
    start = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        icmpmod.ping_many(["192.0.2.10", "192.0.2.11"], count=100, interval=0.2, **kw)
    return time.monotonic() - start


def test_ctrl_c_stops_a_windows_api_sweep_at_once(monkeypatch):
    monkeypatch.delenv("NETDIAG_PING_METHOD", raising=False)
    monkeypatch.setattr(icmpmod, "icmp_socket_available", lambda: False)
    monkeypatch.setattr(icmpmod, "win_icmp_available", lambda: True)
    monkeypatch.setattr(winicmp, "get_api", _EchoApi)
    assert _interrupted_sweep(timeout=0.2) < 2.0  # the sweep alone takes 20 s


def test_ctrl_c_stops_a_system_ping_sweep_at_once(monkeypatch):
    monkeypatch.setenv("NETDIAG_PING_METHOD", "system")
    monkeypatch.setattr(icmpmod, "have", lambda cmd: True)
    monkeypatch.setattr(icmpmod, "run", lambda args, timeout: time.sleep(30))
    assert _interrupted_sweep() < 3.0  # each ping would run for 30 s


# ------------------------------------------------- old iputils, BusyBox, macOS


def _silent_device_ping(rejects):
    """A POSIX ping (reconstructed) that rejects *rejects* and gets no replies.

    It reports "timed out" when its run would outlast the caller's limit, as
    netdiag.core.util.run does: ping waits ``-i`` (default 1 s) between
    requests and lingers ``-W`` (default 10 s) for the last reply.
    """
    calls = []

    def fake_run(args, timeout):
        calls.append(args)
        if rejects in args:
            usage = (
                f"ping: invalid option -- '{rejects[1:]}'\nUsage: ping [OPTIONS] HOST\n"
            )
            return CommandResult(False, 2, "", usage, True)
        count = int(args[args.index("-c") + 1])
        pace = float(args[args.index("-i") + 1]) if "-i" in args else 1.0
        linger = float(args[args.index("-W") + 1]) if "-W" in args else 10.0
        if count * pace + linger > timeout:
            return CommandResult(False, 124, "", "timed out", True, timed_out=True)
        host = args[-1]
        out = (
            f"PING {host} ({host}) 56(84) bytes of data.\n\n"
            f"--- {host} ping statistics ---\n"
            f"{count} packets transmitted, 0 received, 100% packet loss, time 1800ms\n"
        )
        return CommandResult(False, 1, out, "", True)

    return calls, fake_run


@pytest.mark.parametrize(
    "flavour, rejects",
    [("old iputils / inetutils", "-4"), ("BusyBox", "-n")],
)
def test_silent_device_on_an_old_ping_is_no_reply_not_cannot_measure(
    monkeypatch, flavour, rejects
):
    calls, fake_run = _silent_device_ping(rejects)
    monkeypatch.setattr(icmpmod, "is_windows", lambda: False)
    monkeypatch.setattr(icmpmod, "is_macos", lambda: False)
    monkeypatch.setattr(icmpmod, "have", lambda cmd: cmd == "ping")
    monkeypatch.setattr(icmpmod, "run", fake_run)
    st = icmpmod.system_ping("192.0.2.10", count=10, family=4)
    assert st.error is None, flavour  # was "timed out": SKIP instead of no reply
    assert (st.sent, st.received) == (10, 0)
    assert rejects not in calls[1] and "-i" in calls[1] and "-W" in calls[1]


def test_macos_ping6_gets_arguments_kame_ping6_accepts():
    args = icmpmod.build_ping_args(
        "2001:db8::10", 10, 1.0, 0.2, size=32, family=6, os_name="macos"
    )
    assert args[0] == "ping6" and "-W" not in args  # -W takes no value there
    assert float(args[args.index("-i") + 1]) >= 1.0  # below 1 s needs root
    assert args[-1] == "2001:db8::10"
