"""Parsers for traceroute and ping output (no real network / subprocess).

The ping fixtures are verbatim formats of iputils (Linux, C locale), macOS
and BusyBox.
"""

from netdiag.core.util import CommandResult
from netdiag.measure import icmp as pingmod
from netdiag.measure.icmp import (
    build_ping_args,
    parse_posix_ping,
    system_ping,
)
from netdiag.probes import icmp
from netdiag.probes.traceroute import _parse_hops

# ---------------------------------------------------------------- traceroute


def test_parse_hops_basic():
    text = "\n".join(
        [
            " 1  10.0.0.1  1.234 ms  1.111 ms",
            " 2  * * *",
            " 3  203.0.113.5  10.5 ms",
        ]
    )
    hops = _parse_hops(text)

    assert hops == [
        {"hop": 1, "ip": "10.0.0.1", "rtt_ms": 1.17, "timeout": False},
        {"hop": 2, "ip": None, "rtt_ms": None, "timeout": True},
        {"hop": 3, "ip": "203.0.113.5", "rtt_ms": 10.5, "timeout": False},
    ]


def test_parse_hops_upgrades_timed_out_hop_to_resolved():
    # Same hop number appears twice: first a timeout, then a resolved line.
    # The resolved entry must replace the timed-out one.
    text = "\n".join(
        [
            " 4  *",
            " 4  198.51.100.7  20.0 ms",
        ]
    )
    hops = _parse_hops(text)

    assert hops == [{"hop": 4, "ip": "198.51.100.7", "rtt_ms": 20.0, "timeout": False}]


# --------------------------------------------------------------- posix ping

IPUTILS_OK = """\
PING 192.168.1.1 (192.168.1.1) 56(84) bytes of data.
64 bytes from 192.168.1.1: icmp_seq=1 ttl=64 time=0.456 ms
64 bytes from 192.168.1.1: icmp_seq=1 ttl=64 time=0.789 ms (DUP!)
64 bytes from 192.168.1.1: icmp_seq=2 ttl=64 time=0.512 ms
From 192.168.1.10 icmp_seq=3 Destination Host Unreachable

--- 192.168.1.1 ping statistics ---
3 packets transmitted, 2 received, +1 duplicates, +1 errors, 33.3333% packet loss, time 2003ms
rtt min/avg/max/mdev = 0.456/0.584/0.789/0.166 ms
"""

IPUTILS_DOWN = """\
PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.

--- 8.8.8.8 ping statistics ---
3 packets transmitted, 0 received, 100% packet loss, time 2000ms
"""

MACOS = """\
PING 192.168.1.1 (192.168.1.1): 56 data bytes
64 bytes from 192.168.1.1: icmp_seq=0 ttl=64 time=2.000 ms
Request timeout for icmp_seq 1
64 bytes from 192.168.1.1: icmp_seq=2 ttl=64 time=3.000 ms

--- 192.168.1.1 ping statistics ---
3 packets transmitted, 2 packets received, 33.3% packet loss
round-trip min/avg/max/stddev = 2.000/2.500/3.000/0.500 ms
"""

BUSYBOX = """\
PING 8.8.8.8 (8.8.8.8): 56 data bytes
64 bytes from 8.8.8.8: seq=0 ttl=117 time=10.123 ms
64 bytes from 8.8.8.8: seq=0 ttl=117 time=10.456 ms (DUP!)

--- 8.8.8.8 ping statistics ---
2 packets transmitted, 1 packets received, 1 duplicates, 50% packet loss
round-trip min/avg/max = 10.123/10.289/10.456 ms
"""


def test_iputils_counts_replies_dups_and_errors():
    sent, received, dups, rtts, errors = parse_posix_ping(IPUTILS_OK, count=3)
    assert (sent, received, dups) == (3, 2, 1)
    assert rtts == [0.456, 0.512]  # the DUP reply is not a second sample
    assert errors == {"Destination Host Unreachable": 1}


def test_iputils_total_loss():
    sent, received, dups, rtts, _ = parse_posix_ping(IPUTILS_DOWN, count=3)
    assert (sent, received, dups, rtts) == (3, 0, 0, [])


def test_macos_summary_wins_over_timeout_lines():
    sent, received, _dups, rtts, _ = parse_posix_ping(MACOS, count=3)
    assert (sent, received) == (3, 2)
    assert rtts == [2.0, 3.0]


def test_busybox_dup_line_counted_even_without_plus_sign():
    sent, received, dups, rtts, _ = parse_posix_ping(BUSYBOX, count=2)
    assert (sent, received, dups) == (2, 1, 1)
    assert rtts == [10.123]


# ------------------------------------------------------------- system_ping


def _canned(monkeypatch, stdout):
    monkeypatch.setattr(pingmod, "have", lambda cmd: True)
    calls = []

    def fake_run(args, timeout):
        calls.append(args)
        return CommandResult(ok=True, code=0, stdout=stdout, stderr="", found=True)

    monkeypatch.setattr(pingmod, "run", fake_run)
    return calls


def test_system_ping_success_statistics(monkeypatch):
    _canned(monkeypatch, IPUTILS_OK)
    st = system_ping("192.168.1.1", count=3)
    assert st.method == "system-ping"
    assert (st.sent, st.received, st.duplicates) == (3, 2, 1)
    assert st.error is None
    assert round(st.loss_pct, 1) == 33.3
    assert st.rtt.median == 0.484


def test_system_ping_permission_error_is_not_a_down_device(monkeypatch):
    _canned(monkeypatch, "ping: permission denied (are you root?)\n")
    st = system_ping("192.168.1.1", count=3)
    assert st.error and "permission denied" in st.error
    assert st.received == 0


def test_build_ping_args_per_platform():
    linux = build_ping_args("h", count=5, timeout=1.0, interval=0.2, os_name="linux")
    assert linux[:3] == ["ping", "-n", "-c"] and "-W" in linux and linux[-1] == "h"
    assert linux[linux.index("-i") + 1] == "0.2"
    mac = build_ping_args("h", count=5, timeout=1.0, interval=0.05, os_name="macos")
    assert mac[mac.index("-i") + 1] == "0.1"  # non-root floor on macOS
    assert mac[mac.index("-W") + 1] == "1000"  # milliseconds on macOS


def test_icmp_ping_compat_wrapper(monkeypatch):
    # The legacy (avg_rtt, loss_pct, raw) API is kept for external callers.
    _canned(monkeypatch, IPUTILS_DOWN)
    monkeypatch.setenv("NETDIAG_PING_METHOD", "system")
    rtt, loss, raw = icmp.ping("8.8.8.8", count=3, timeout=2.0)
    assert rtt is None
    assert loss == 100.0
    assert raw == IPUTILS_DOWN
