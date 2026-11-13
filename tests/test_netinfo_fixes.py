"""How netdiag reads this PC's interfaces, addresses, routes and neighbours.

Windows and macOS samples are reconstructed from the documented output
formats; the Japanese Windows text is encoded in cp932 with CRLF line ends and
decoded the way ``run()`` decodes it.
"""

import json

import pytest

from netdiag import probes  # noqa: F401  (registers the probes)
from netdiag.diagnosis.engine import Engine
from netdiag.measure import PingStats
from netdiag.netinfo import linux, windows
from netdiag.netinfo.model import KIND_ETHERNET, HostNetwork
from netdiag.probes import gateway as gateway_probe


def _run(hn, only, **config):
    return {
        r.name: r
        for r in Engine(config={"_netinfo": hn, **config}).run(only=only).results
    }


@pytest.fixture
def pinged(monkeypatch):
    """Answer every gateway ping and record which addresses were pinged."""
    seen = []

    def fake_ping(target, **kw):
        seen.append(target)
        return PingStats(
            target=target, address=target, sent=4, received=4, rtts_ms=[1.0] * 4
        )

    monkeypatch.setattr(gateway_probe, "ping", fake_ping)
    return seen


def _linux(addr, routes, base):
    return HostNetwork(
        interfaces=linux.parse_addr_json(addr, base=base),
        routes=linux.parse_route_json(routes),
        source="linux:ip-json",
    )


def _adapter(name, index, desc, status="Up", media="802.3", mac=None, **extra):
    return {
        "Name": name,
        "InterfaceDescription": desc,
        "ifIndex": index,
        "Status": status,
        "MacAddress": mac if mac is not None else f"00-00-5E-00-53-{index:02X}",
        "LinkSpeed": "1 Gbps" if status == "Up" else "0 bps",
        "ReceiveLinkSpeed": 1000000000 if status == "Up" else 0,
        "FullDuplex": True,
        "MtuSize": 1500,
        "Virtual": False,
        "HardwareInterface": True,
        "MediaType": media,
        "PhysicalMediaType": media,
        **extra,
    }


def _address(ip, plen, index, alias, state="Preferred", origin="Manual"):
    return {
        "IPAddress": ip,
        "PrefixLength": plen,
        "InterfaceIndex": index,
        "InterfaceAlias": alias,
        "AddressState": state,
        "PrefixOrigin": origin,
    }


def _route(dst, hop, index, alias, route_metric, if_metric):
    return {
        "DestinationPrefix": dst,
        "NextHop": hop,
        "InterfaceIndex": index,
        "InterfaceAlias": alias,
        "RouteMetric": route_metric,
        "InterfaceMetric": if_metric,
    }


def _windows(**sections):
    """PowerShell COLLECT_SCRIPT output, round-tripped through JSON text."""
    data = {"adapters": [], "addresses": [], "routes": [], "dns": [], "stats": []}
    data.update(sections)
    return windows.parse_collect_json(json.loads(json.dumps(data, ensure_ascii=False)))


def _sysfs_nic(base, name, counters):
    nic = base / name
    (nic / "device").mkdir(parents=True)
    (nic / "statistics").mkdir()
    (nic / "carrier").write_text("1\n")
    (nic / "speed").write_text("1000\n")
    (nic / "duplex").write_text("full\n")
    for key, value in counters.items():
        (nic / "statistics" / key).write_text(f"{value}\n")


def _eth_json(name, ip, plen, index=2, flags=("BROADCAST", "MULTICAST")):
    return {
        "ifindex": index,
        "ifname": name,
        "flags": [*flags, "UP", "LOWER_UP"],
        "operstate": "UP",
        "link_type": "ether",
        "address": f"00:00:5e:00:53:{index:02x}",
        "addr_info": [{"family": "inet", "local": ip, "prefixlen": plen}],
    }


# ------------------------------------------------------------- NIC counters


def test_crc_errors_are_counted_once(tmp_path):
    _sysfs_nic(
        tmp_path,
        "eth0",
        {
            "rx_packets": 5000,
            "tx_packets": 5000,
            "rx_errors": 7,
            "tx_errors": 0,
            "rx_crc_errors": 7,
            "collisions": 0,
        },
    )
    hn = HostNetwork(
        interfaces=linux.parse_addr_json(
            [_eth_json("eth0", "192.0.2.10", 24)], base=tmp_path
        )
    )
    assert hn.interfaces[0].kind == KIND_ETHERNET
    link = _run(hn, ["link"])["link"]
    assert link.code == "link.errors"
    assert "7 errors since boot" in link.summary


# ------------------------------------------------------------ default route


def test_metric_0_static_default_beats_a_dhcp_wifi_default(tmp_path, pinged):
    # `ip route add default via ...` and ifupdown give metric 0, which
    # `ip -j route` leaves out; NetworkManager's Wi-Fi default has 600.
    hn = _linux(
        [
            _eth_json("eth0", "192.0.2.5", 24, index=2),
            _eth_json("wlan0", "198.51.100.20", 24, index=3),
        ],
        [
            {"dst": "default", "gateway": "192.0.2.1", "dev": "eth0", "flags": []},
            {
                "dst": "default",
                "gateway": "198.51.100.1",
                "dev": "wlan0",
                "protocol": "dhcp",
                "metric": 600,
                "flags": [],
            },
            {"dst": "192.0.2.0/24", "dev": "eth0", "scope": "link", "flags": []},
            {
                "dst": "198.51.100.0/24",
                "dev": "wlan0",
                "scope": "link",
                "metric": 600,
                "flags": [],
            },
        ],
        tmp_path,
    )
    res = _run(hn, ["gateway", "ipconfig"], internet=True)
    assert pinged == ["192.0.2.1"]
    assert res["gateway"].detail["gateway"] == "192.0.2.1"
    assert "192.0.2.1 normally wins" in res["ipconfig"].summary
    assert hn.decide_route("203.0.113.9").gateway == "192.0.2.1"


def test_text_route_without_metric_reads_as_metric_0():
    routes = linux.parse_route_text(
        "default via 198.51.100.1 dev wlan0 proto dhcp metric 600\n"
        "default via 192.0.2.1 dev eth0\n"
    )
    hn = HostNetwork(routes=routes)
    assert hn.preferred_default_route().gateway == "192.0.2.1"


def _two_gateway_laptop():
    # Get-NetRoute lists equal prefixes by interface, not by metric: the Wi-Fi
    # default (35) comes first, the equipment NIC's typed gateway (25) second.
    return _windows(
        adapters=[
            _adapter("イーサネット", 12, "Intel(R) Ethernet Connection I219-LM"),
            _adapter("Wi-Fi", 18, "Intel(R) Wi-Fi 6E AX211", media="Native 802.11"),
        ],
        addresses=[
            _address("192.0.2.100", 24, 12, "イーサネット"),
            _address("198.51.100.40", 24, 18, "Wi-Fi", origin="Dhcp"),
        ],
        routes=[
            _route("0.0.0.0/0", "198.51.100.1", 18, "Wi-Fi", 0, 35),
            _route("0.0.0.0/0", "192.0.2.1", 12, "イーサネット", 0, 25),
            _route("192.0.2.0/24", "0.0.0.0", 12, "イーサネット", 256, 25),
            _route("198.51.100.0/24", "0.0.0.0", 18, "Wi-Fi", 256, 35),
        ],
    )


def test_route_and_gateway_checks_name_the_same_gateway(pinged):
    hn = _two_gateway_laptop()
    res = _run(
        hn,
        ["gateway", "route"],
        internet=True,
        target={"name": "PLC-B", "host": "203.0.113.5"},
    )
    assert pinged == ["192.0.2.1"]
    assert res["route"].detail["gateway"] == "192.0.2.1"
    assert res["route"].summary == "routed via gateway 192.0.2.1"
