"""DNS probe and the built-in DNS client.

* A scenario's ``resolver:`` (``ctx["_resolver"]``) is queried directly and the
  probe never silently falls back to the system configuration (issue #35).
* Each configured resolver is queried individually, so a dead secondary DNS
  server shows up as a warning even though names still resolve.
* The stdlib DNS client builds/parses real wire-format packets (no
  ``nslookup``).
"""

import socket
import struct
import threading

from netdiag.core import Context, Status
from netdiag.measure import dns as dnsmod
from netdiag.measure.dns import DnsAnswer, build_query, parse_response, query
from netdiag.probes import dns
from netdiag.probes.dns import DnsProbe


def _answer(server, addrs=("93.184.216.34",), error=None, rcode="NOERROR", ms=12.0):
    return DnsAnswer(
        server=server,
        name="example.com",
        addresses=list(addrs) if not error else [],
        rcode=None if error else rcode,
        elapsed_ms=None if error else ms,
        error=error,
    )


# ------------------------------------------------------------------ custom path


def test_dns_uses_custom_resolver_when_set(monkeypatch):
    calls = []

    def fake_query(name, server, timeout=2.0):
        calls.append((name, server))
        return _answer(server)

    monkeypatch.setattr(dns, "query", fake_query)
    res = DnsProbe().check(Context({"_resolver": "8.8.8.8", "dns_name": "example.com"}))

    assert res.status == Status.OK
    assert res.detail["resolver"] == "8.8.8.8"
    assert "93.184.216.34" in res.detail["answers"]
    assert calls == [("example.com", "8.8.8.8")]


def test_dns_custom_resolver_fail_path(monkeypatch):
    monkeypatch.setattr(
        dns,
        "query",
        lambda name, server, timeout=2.0: _answer(server, addrs=(), rcode="NXDOMAIN"),
    )
    res = DnsProbe().check(
        Context({"_resolver": "8.8.8.8", "dns_name": "nope.invalid"})
    )
    assert res.status == Status.FAIL
    assert "resolver 8.8.8.8" in res.summary
    assert "NXDOMAIN" in res.summary


def test_dns_custom_resolver_timeout_is_reported(monkeypatch):
    monkeypatch.setattr(
        dns, "query", lambda name, server, timeout=2.0: _answer(server, error="timeout")
    )
    res = DnsProbe().check(Context({"_resolver": "10.0.0.53", "dns_name": "x"}))
    assert res.status == Status.FAIL
    assert "timeout" in res.summary


# ------------------------------------------------------------------ default path


def test_dns_default_path_uses_system_resolver(monkeypatch):
    used = {"system": False}

    def fake_resolve(name, timeout=3.0):
        used["system"] = True
        return ["1.2.3.4"]

    monkeypatch.setattr(dns, "_resolve", fake_resolve)
    monkeypatch.setattr(dns, "configured_resolvers", lambda: [])
    res = DnsProbe().check(Context({"dns_name": "example.com"}))
    assert used["system"] is True
    assert res.status == Status.OK
    assert res.code == "dns.ok"


def test_dns_warns_when_one_configured_server_is_dead(monkeypatch):
    monkeypatch.setattr(dns, "_resolve", lambda name, timeout=3.0: ["1.2.3.4"])
    monkeypatch.setattr(dns, "configured_resolvers", lambda: ["10.0.0.53", "10.0.0.54"])
    answers = {
        "10.0.0.53": _answer("10.0.0.53"),
        "10.0.0.54": _answer("10.0.0.54", error="timeout"),
    }
    monkeypatch.setattr(dns, "query", lambda name, server, timeout=2.0: answers[server])
    res = DnsProbe().check(Context({"dns_name": "example.com"}))
    assert res.status == Status.WARN
    assert res.code == "dns.server_down"
    assert "10.0.0.54" in res.summary


def test_dns_failure_names_each_server(monkeypatch):
    monkeypatch.setattr(dns, "_resolve", lambda name, timeout=3.0: None)
    monkeypatch.setattr(dns, "configured_resolvers", lambda: ["10.0.0.53"])
    monkeypatch.setattr(
        dns, "query", lambda name, server, timeout=2.0: _answer(server, error="timeout")
    )
    res = DnsProbe().check(Context({"dns_name": "intranet.corp"}))
    assert res.status == Status.FAIL
    assert "10.0.0.53" in res.summary


# --------------------------------------------------------------- wire format


def _response(qid, name, addrs, rcode=0):
    question = build_query(name, qid)[12:]
    header = struct.pack("!HHHHHH", qid, 0x8180 | rcode, 1, len(addrs), 0, 0)
    answers = b""
    for a in addrs:
        answers += b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton(a)
    return header + question + answers


def test_build_and_parse_roundtrip():
    q = build_query("plc.line1.local", 0x1234)
    assert q[:2] == b"\x12\x34"
    assert b"\x03plc\x05line1\x05local\x00" in q
    rcode, addrs = parse_response(
        _response(0x1234, "plc.line1.local", ["10.0.0.5", "10.0.0.6"]), 0x1234
    )
    assert rcode == "NOERROR"
    assert addrs == ["10.0.0.5", "10.0.0.6"]


def test_parse_nxdomain_and_id_mismatch():
    rcode, addrs = parse_response(_response(7, "x.invalid", [], rcode=3), 7)
    assert (rcode, addrs) == ("NXDOMAIN", [])
    try:
        parse_response(_response(7, "x", []), 8)
    except ValueError:
        pass
    else:
        raise AssertionError("an id mismatch must be rejected")


def test_query_against_local_udp_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]

    def serve():
        data, addr = srv.recvfrom(512)
        qid = struct.unpack("!H", data[:2])[0]
        srv.sendto(_response(qid, "plc.local", ["192.168.3.39"]), addr)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        ans = query("plc.local", "127.0.0.1", timeout=2.0, port=port)
    finally:
        t.join(2)
        srv.close()
    assert ans.ok
    assert ans.addresses == ["192.168.3.39"]
    assert ans.elapsed_ms is not None and ans.elapsed_ms >= 0


def test_query_timeout_when_nobody_answers():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))  # bound but never answers
    port = s.getsockname()[1]
    try:
        ans = dnsmod.query("x.local", "127.0.0.1", timeout=0.3, port=port)
    finally:
        s.close()
    assert not ans.ok
    assert ans.error == "timeout"
