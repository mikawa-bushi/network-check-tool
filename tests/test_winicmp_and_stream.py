"""Windows ICMP API pinger (with a fake API), streaming pings, per-request results.

The real ``iphlpapi`` calls only exist on Windows (exercised by the Windows CI
job); everything around them — pacing, loss accounting, duplicate answers,
"unreachable" statuses, ARP verdicts — is tested here with a fake.
"""

import ctypes
import socket
import struct
import sys
import threading
import time

import pytest

from netdiag.measure import icmp as icmpmod
from netdiag.measure import winicmp
from netdiag.measure.icmp import PingStats, per_request_results


class FakeApi:
    """Scripted IcmpSendEcho: per-address list of (status, replies)."""

    def __init__(self, script, arp=None):
        self.script = {a: list(v) for a, v in script.items()}
        self.arp = arp or {}
        self.opened = self.closed = 0
        self.calls = []

    def open(self):
        self.opened += 1
        return self.opened

    def close(self, handle):
        self.closed += 1

    def echo(self, handle, ip, payload, timeout_ms):
        self.calls.append((ip, len(payload), timeout_ms))
        seq = self.script.get(ip) or [(winicmp.IP_REQ_TIMED_OUT, 0)]
        status, replies = seq.pop(0) if len(seq) > 1 else seq[0]
        return status, replies, 0

    def send_arp(self, ip):
        return self.arp.get(ip, (winicmp.ERROR_BAD_NET_NAME, None))


def test_struct_layout_and_address_conversion():
    expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    assert ctypes.sizeof(winicmp.ICMP_ECHO_REPLY) == expected
    # IPAddr holds network-order bytes in memory, whatever the host endianness.
    raw = struct.pack("=I", winicmp.ipaddr("192.168.3.39"))
    assert socket.inet_ntoa(raw) == "192.168.3.39"


def test_fixed_count_accounting():
    api = FakeApi(
        {
            "10.0.0.1": [(winicmp.IP_SUCCESS, 1)],
            "10.0.0.2": [(11003, 1)],  # destination host unreachable: a loss
            "10.0.0.3": [(winicmp.IP_SUCCESS, 2)],  # two answers: IP conflict
        }
    )
    jobs = {"a": "10.0.0.1", "b": "10.0.0.2", "c": "10.0.0.3"}
    stats = winicmp.WinIcmpPinger(
        jobs, interval=0.01, timeout=0.2, size=32, max_pps=1000, count=5, api=api
    ).run()
    assert (stats["a"].sent, stats["a"].received, stats["a"].method) == (
        5,
        5,
        "win-icmp",
    )
    assert len(stats["a"].rtts_ms) == 5 and stats["a"].resolution_ms < 1
    assert (stats["b"].sent, stats["b"].received) == (5, 0)
    assert stats["b"].errors == {"destination host unreachable": 5}
    assert stats["c"].duplicates == 5
    assert api.opened == api.closed == 3  # one handle per address, always closed
    assert all(size >= 8 and timeout == 200 for _ip, size, timeout in api.calls)


def test_timeouts_are_losses_without_error_noise():
    api = FakeApi({"10.0.0.9": [(winicmp.IP_REQ_TIMED_OUT, 0)]})
    st = winicmp.WinIcmpPinger(
        {"x": "10.0.0.9"},
        interval=0.01,
        timeout=0.1,
        size=32,
        max_pps=0,
        count=3,
        api=api,
    ).run()["x"]
    assert (st.sent, st.received, st.errors) == (3, 0, {})


def test_streaming_reports_every_request_until_stopped():
    api = FakeApi({"10.0.0.1": [(winicmp.IP_SUCCESS, 1)], "10.0.0.2": [(11010, 0)]})
    samples = []
    stop = threading.Event()
    pinger = winicmp.WinIcmpPinger(
        {"a": "10.0.0.1", "b": "10.0.0.2"},
        interval=0.02,
        timeout=0.1,
        size=32,
        max_pps=1000,
        on_sample=lambda *s: samples.append(s),
        stop=stop,
        api=api,
    )
    t = threading.Thread(target=pinger.run)
    t.start()
    time.sleep(0.25)
    stop.set()
    t.join(2)
    assert not t.is_alive()
    a = [s for s in samples if s[0] == "a"]
    b = [s for s in samples if s[0] == "b"]
    assert len(a) >= 5 and all(s[2] is not None for s in a)
    assert len(b) >= 5 and all(s[2] is None and s[3] == "request timed out" for s in b)
    assert all(abs(s[1] - time.time()) < 5 for s in samples)  # wall-clock send times


def test_rate_limiter_caps_requests_per_second():
    lim = winicmp.RateLimiter(100)
    start = time.perf_counter()
    for _ in range(11):
        lim.acquire()
    assert time.perf_counter() - start >= 0.09


def test_arp_lookup_verdicts():
    api = FakeApi(
        {}, arp={"10.0.0.1": (0, "00:26:92:aa:bb:cc"), "10.0.0.3": (31, None)}
    )
    assert winicmp.arp_lookup("10.0.0.1", api=api) == ("present", "00:26:92:aa:bb:cc")
    assert winicmp.arp_lookup("10.0.0.2", api=api) == ("absent", None)  # bad net name
    assert winicmp.arp_lookup("10.0.0.3", api=api) == ("unknown", None)


def test_backend_choice_and_override(monkeypatch):
    monkeypatch.setattr(icmpmod, "icmp_socket_available", lambda: False)
    monkeypatch.setattr(icmpmod, "win_icmp_available", lambda: True)
    assert icmpmod._choose("auto", 4) == "win"
    assert icmpmod._choose("auto", 6) == "system"  # IPv6 goes through ping.exe
    monkeypatch.setenv("NETDIAG_PING_METHOD", "system")
    assert icmpmod._choose("auto", 4) == "system"


def test_per_request_results_place_losses(monkeypatch):
    monkeypatch.setattr(icmpmod, "is_windows", lambda: False)
    posix = PingStats(
        target="h",
        raw="64 bytes from h: icmp_seq=1 ttl=64 time=0.5 ms\n"
        "64 bytes from h: icmp_seq=3 ttl=64 time=0.7 ms\n"
        "64 bytes from h: icmp_seq=3 ttl=64 time=0.9 ms (DUP!)\n",
    )
    assert per_request_results(posix, 4) == [0.5, None, 0.7, None]
    monkeypatch.setattr(icmpmod, "is_windows", lambda: True)
    win = PingStats(
        target="h",
        raw="\nh に ping を送信しています 32 バイトのデータ:\n"
        "h からの応答: バイト数 =32 時間 <1ms TTL=64\n"
        "要求がタイムアウトしました。\n"
        "h からの応答: バイト数 =32 時間 =3ms TTL=64\n\n統計\n",
    )
    assert per_request_results(win, 3) == [0.5, None, 3.0]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ICMP API")
def test_live_windows_icmp_loopback():
    stats = icmpmod.ping_many(["127.0.0.1"], count=5, interval=0.05, timeout=0.5)
    st = stats["127.0.0.1"]
    assert st.method == "win-icmp"
    assert (st.sent, st.received) == (5, 5)
    assert st.rtt.max < 50


@pytest.mark.skipif(
    not icmpmod.precise_ping_available(), reason="no unprivileged ICMP on this host"
)
def test_live_stream_loopback():
    samples = []
    stop = threading.Event()
    t = threading.Thread(
        target=icmpmod.ping_stream,
        kwargs=dict(
            targets=["127.0.0.1"],
            on_sample=lambda *s: samples.append(s),
            stop=stop,
            interval=0.02,
            timeout=0.3,
        ),
    )
    t.start()
    time.sleep(0.3)
    stop.set()
    t.join(3)
    assert len(samples) >= 5 and all(s[2] is not None for s in samples)
