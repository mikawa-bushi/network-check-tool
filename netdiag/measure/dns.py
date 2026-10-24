"""Minimal DNS client (UDP, A records) on the standard library.

Querying a specific resolver directly — instead of shelling out to ``nslookup``,
whose output is localised on Windows — gives a precise per-server answer:
which configured DNS server answers, how fast, and with which response code.
"""

from __future__ import annotations

import os
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

RCODES = {
    0: "NOERROR",
    1: "FORMERR",
    2: "SERVFAIL",
    3: "NXDOMAIN",
    4: "NOTIMP",
    5: "REFUSED",
}


@dataclass
class DnsAnswer:
    server: str
    name: str
    addresses: List[str] = field(default_factory=list)
    rcode: Optional[str] = None
    elapsed_ms: Optional[float] = None
    error: Optional[str] = None  # timeout / network error (no response at all)

    @property
    def ok(self) -> bool:
        return self.error is None and self.rcode == "NOERROR" and bool(self.addresses)

    def to_dict(self) -> dict:
        return {
            "server": self.server,
            "name": self.name,
            "addresses": list(self.addresses),
            "rcode": self.rcode,
            "elapsed_ms": (
                round(self.elapsed_ms, 2) if self.elapsed_ms is not None else None
            ),
            "error": self.error,
        }


def build_query(name: str, qid: int, qtype: int = 1) -> bytes:
    header = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0)  # RD=1, one question
    qname = b"".join(
        bytes([len(label)]) + label
        for label in (p.encode("idna") for p in name.rstrip(".").split("."))
        if label
    )
    return header + qname + b"\x00" + struct.pack("!HH", qtype, 1)


def _skip_name(data: bytes, offset: int) -> int:
    while True:
        if offset >= len(data):
            raise ValueError("truncated name")
        length = data[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:  # compression pointer
            return offset + 2
        offset += 1 + length


def parse_response(data: bytes, qid: int) -> Tuple[str, List[str]]:
    """Return ``(rcode name, IPv4 answers)``; raises ValueError if malformed."""
    if len(data) < 12:
        raise ValueError("short response")
    rid, flags, qd, an, _ns, _ar = struct.unpack("!HHHHHH", data[:12])
    if rid != qid:
        raise ValueError("id mismatch")
    rcode = RCODES.get(flags & 0x000F, str(flags & 0x000F))
    offset = 12
    for _ in range(qd):
        offset = _skip_name(data, offset) + 4
    addrs: List[str] = []
    for _ in range(an):
        offset = _skip_name(data, offset)
        rtype, _rclass, _ttl, rdlen = struct.unpack("!HHIH", data[offset : offset + 10])
        offset += 10
        rdata = data[offset : offset + rdlen]
        offset += rdlen
        if rtype == 1 and rdlen == 4:
            addrs.append(socket.inet_ntoa(rdata))
    return rcode, addrs


def query(name: str, server: str, timeout: float = 2.0, port: int = 53) -> DnsAnswer:
    """Ask *server* for the A records of *name* (one UDP round trip)."""
    ans = DnsAnswer(server=server, name=name)
    qid = struct.unpack("!H", os.urandom(2))[0]
    try:
        packet = build_query(name, qid)
    except UnicodeError as e:
        ans.error = f"invalid name: {e}"
        return ans
    try:
        fam = socket.AF_INET6 if ":" in server else socket.AF_INET
        with socket.socket(fam, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            start = time.perf_counter()
            s.sendto(packet, (server, port))
            deadline = start + timeout
            while True:
                s.settimeout(max(0.01, deadline - time.perf_counter()))
                data, _addr = s.recvfrom(4096)
                try:
                    ans.rcode, ans.addresses = parse_response(data, qid)
                except ValueError:
                    continue  # stray/garbled datagram: keep waiting for ours
                ans.elapsed_ms = (time.perf_counter() - start) * 1000.0
                return ans
    except (socket.timeout, TimeoutError):
        ans.error = "timeout"
    except OSError as e:
        ans.error = e.strerror or str(e)
    return ans
