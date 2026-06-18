"""Tests for issue #25 (M10 phase 1): probe output parsers.

The probe layer was largely untested. These cover the two output parsers the
review called out, by feeding fixed command output (no real network / subprocess):
``traceroute._parse_hops`` (a pure function) and ``icmp.ping`` (via a stubbed
command runner).
"""

from netdiag.core.util import CommandResult
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


# ---------------------------------------------------------------------- icmp


def _canned(monkeypatch, stdout):
    monkeypatch.setattr(icmp, "is_windows", lambda: False)
    monkeypatch.setattr(
        icmp,
        "run",
        lambda args, timeout: CommandResult(
            ok=True, code=0, stdout=stdout, stderr="", found=True
        ),
    )


def test_ping_parses_success(monkeypatch):
    stdout = "\n".join(
        [
            "PING 1.1.1.1 (1.1.1.1) 56(84) bytes of data.",
            "64 bytes from 1.1.1.1: icmp_seq=1 ttl=58 time=12.3 ms",
            "--- 1.1.1.1 ping statistics ---",
            "1 packets transmitted, 1 received, 0% packet loss, time 0ms",
            "rtt min/avg/max/mdev = 12.345/13.456/14.567/0.500 ms",
        ]
    )
    _canned(monkeypatch, stdout)

    rtt, loss, raw = icmp.ping("1.1.1.1", count=1, timeout=2.0)

    assert rtt == 13.456  # the avg field
    assert loss == 0.0
    assert raw == stdout


def test_ping_parses_total_loss(monkeypatch):
    stdout = "\n".join(
        [
            "PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.",
            "--- 8.8.8.8 ping statistics ---",
            "3 packets transmitted, 0 received, 100% packet loss, time 2000ms",
        ]
    )
    _canned(monkeypatch, stdout)

    rtt, loss, raw = icmp.ping("8.8.8.8", count=3, timeout=2.0)

    assert rtt is None
    assert loss == 100.0
    assert raw == stdout
