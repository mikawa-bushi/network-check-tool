"""Regression tests for the measurement layer: statistics, DNS, TCP and ping."""

import socket
import struct
import threading
import time

import pytest

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
        b"\xc0\x0c"
        + struct.pack("!HHIH", 1, 1, 60, 4)
        + bytes([10, 0, i // 256, i % 256])
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
    assert ans.addresses == ["10.0.0.7"]


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
