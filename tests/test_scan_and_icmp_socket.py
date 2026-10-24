"""Subnet discovery and the unprivileged ICMP socket pinger."""

import ipaddress
import struct

import pytest

from netdiag import netinfo
from netdiag.measure import icmp_socket_available, ping_many
from netdiag.measure.icmp import build_echo_request, parse_echo_reply
from netdiag.netinfo.model import (
    KIND_ETHERNET,
    KIND_VIRTUAL,
    Address,
    HostNetwork,
    Interface,
    Neighbor,
)
from netdiag.scan import default_networks, parse_networks

# ------------------------------------------------------------------ networks


def test_parse_networks_forms():
    nets = parse_networks(["192.168.3.0/24", "192.168.3.10-12", "10.0.0.5"])
    assert nets[0] == ipaddress.ip_network("192.168.3.0/24")
    assert sum(n.num_addresses for n in nets[1:-1]) == 3  # .10 .11 .12
    assert nets[-1] == ipaddress.ip_network("10.0.0.5/32")


def test_default_networks_prefers_physical_and_narrows_big_subnets():
    hn = HostNetwork(
        interfaces=[
            Interface(
                name="eth0",
                kind=KIND_ETHERNET,
                up=True,
                addresses=[Address("10.20.30.40", 16)],
            ),
            Interface(
                name="docker0",
                kind=KIND_VIRTUAL,
                up=True,
                addresses=[Address("172.17.0.1", 16)],
            ),
        ]
    )
    nets, notes = default_networks(hn, max_hosts=512)
    assert nets == [ipaddress.ip_network("10.20.30.0/24")]
    assert notes and "eth0" in notes[0]


def test_scan_finds_ping_and_arp_only_devices(monkeypatch):
    import netdiag.scan as scan_mod
    from netdiag.measure import PingStats

    hn = HostNetwork(
        interfaces=[
            Interface(
                name="eth0",
                kind=KIND_ETHERNET,
                up=True,
                mac="00:11:22:33:44:55",
                addresses=[Address("192.168.3.100", 29)],
            )
        ]
    )
    monkeypatch.setattr(netinfo, "snapshot", lambda max_age=5.0: hn)
    tables = iter(
        [
            [],  # before: empty ARP cache
            [
                Neighbor(
                    "192.168.3.97",
                    mac="00:26:92:aa:bb:cc",
                    state="reachable",
                    confirmed_age=0,
                ),
                Neighbor("192.168.3.98", mac="00:00:0a:11:22:33", state="stale"),
                Neighbor("192.168.3.99", state="failed"),
            ],
        ]
    )
    monkeypatch.setattr(netinfo, "neighbors", lambda: next(tables))

    def fake_ping_many(hosts, count, interval, timeout, **kw):
        out = {h: PingStats(target=h, sent=count, received=0) for h in hosts}
        out["192.168.3.97"] = PingStats(
            target="192.168.3.97", sent=count, received=count, rtts_ms=[0.3] * count
        )
        return out

    monkeypatch.setattr(scan_mod, "ping_many", fake_ping_many)
    res = scan_mod.scan(parse_networks(["192.168.3.96/29"]), count=2, timeout=0.5)
    by_ip = {h.ip: h for h in res.hits}
    assert set(by_ip) == {"192.168.3.97", "192.168.3.98", "192.168.3.100"}
    assert by_ip["192.168.3.97"].replied and by_ip["192.168.3.97"].rtt_ms == 0.3
    assert by_ip[
        "192.168.3.98"
    ].arp_only  # silent to ping, answered ARP during the sweep
    assert by_ip["192.168.3.100"].is_self
    assert [d.host for d in res.to_devices()] == ["192.168.3.97", "192.168.3.98"]


def test_scan_refuses_oversized_ranges(monkeypatch):
    import netdiag.scan as scan_mod

    with pytest.raises(ValueError):
        scan_mod.scan(parse_networks(["10.0.0.0/16"]), max_hosts=512)


# -------------------------------------------------------------- ICMP socket


def test_echo_request_checksum_is_valid():
    pkt = build_echo_request(0x1234, 7, b"payload!")
    total = sum(struct.unpack(f"!{len(pkt) // 2}H", pkt))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    assert total == 0xFFFF  # a correct Internet checksum sums to all ones


def test_parse_echo_reply_with_and_without_ipv4_header():
    icmp = struct.pack("!BBHHH", 0, 0, 0, 0x1234, 7) + b"data"
    assert parse_echo_reply(icmp) == (0x1234, 7, b"data")  # Linux: bare ICMP
    ip_header = bytes([0x45]) + b"\x00" * 19
    assert parse_echo_reply(ip_header + icmp) == (
        0x1234,
        7,
        b"data",
    )  # macOS: with IP header
    request = struct.pack("!BBHHH", 8, 0, 0, 1, 1)
    assert parse_echo_reply(request) is None


@pytest.mark.skipif(
    not icmp_socket_available(), reason="unprivileged ICMP sockets not permitted here"
)
def test_live_socket_ping_of_loopback():
    st = ping_many(["127.0.0.1"], count=5, interval=0.01, timeout=0.5)["127.0.0.1"]
    assert st.method == "icmp-socket"
    assert (st.sent, st.received, st.duplicates) == (5, 5, 0)
    assert st.rtt.max < 50.0
