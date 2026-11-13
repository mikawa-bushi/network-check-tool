"""Regression tests for the measurement layer: statistics, DNS, TCP and ping."""

import socket
import struct
import threading

import pytest

from netdiag.measure.dns import DnsReplyError, query
from netdiag.measure.stats import summarize


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
