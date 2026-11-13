"""Windows and macOS samples here are reconstructed, not captured on real machines."""

import json
import socket
import struct

import pytest

from netdiag.core.util import CommandResult, decode_output
from netdiag.diagnosis.engine import Engine
from netdiag.measure import PingStats
from netdiag.netinfo import linux, macos, windows
from netdiag.netinfo.model import KIND_ETHERNET, NEIGH_STALE, HostNetwork, Neighbor
from netdiag.probes import gateway as gateway_probe


def _run(hn, only, **config):
    return {
        r.name: r
        for r in Engine(config={"_netinfo": hn, **config}).run(only=only).results
    }


@pytest.fixture
def answered_pings(monkeypatch):
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
    data = {"adapters": [], "addresses": [], "routes": [], "dns": [], "stats": []}
    data.update(sections)
    return windows.parse_collect_json(json.loads(json.dumps(data, ensure_ascii=False)))


@pytest.fixture
def linux_host(monkeypatch, tmp_path):
    outputs = {}
    sysfs = tmp_path / "sys"
    sysfs.mkdir()

    def fake_run(args, timeout=10.0):
        out = outputs.get(tuple(args))
        if out is None:
            return CommandResult(ok=False, code=1, stdout="", stderr="", found=True)
        return CommandResult(ok=True, code=0, stdout=out, stderr="", found=True)

    monkeypatch.setattr(linux, "have", lambda cmd: cmd == "ip")
    monkeypatch.setattr(linux, "run", fake_run)
    monkeypatch.setattr(linux, "SYS_NET", sysfs)
    monkeypatch.setattr(linux, "resolvers", lambda: [])
    return outputs, sysfs


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


def test_metric_0_static_default_beats_a_dhcp_wifi_default(tmp_path, answered_pings):
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
    assert answered_pings == ["192.0.2.1"]
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


def test_route_and_gateway_checks_name_the_same_gateway(answered_pings):
    hn = _two_gateway_laptop()
    res = _run(
        hn,
        ["gateway", "route"],
        internet=True,
        target={"name": "PLC-B", "host": "203.0.113.5"},
    )
    assert answered_pings == ["192.0.2.1"]
    assert res["route"].detail["gateway"] == "192.0.2.1"
    assert res["route"].summary == "routed via gateway 192.0.2.1"


def _p2p_json(name, ip, index, link_type="none"):
    return {
        "ifindex": index,
        "ifname": name,
        "flags": ["POINTOPOINT", "MULTICAST", "NOARP", "UP", "LOWER_UP"],
        "operstate": "UNKNOWN",
        "link_type": link_type,
        "addr_info": [{"family": "inet", "local": ip, "prefixlen": 32}],
    }


def test_linux_tunnel_and_ppp_routes_are_not_arp_links(tmp_path):
    hn = _linux(
        [
            _eth_json("eth0", "192.0.2.20", 25, index=2),
            _p2p_json("wg0", "192.0.2.130", 7),
            _p2p_json("ppp0", "192.0.2.140", 9, link_type="ppp"),
        ],
        [
            {"dst": "default", "dev": "ppp0", "scope": "link", "flags": []},
            {"dst": "203.0.113.0/24", "dev": "wg0", "scope": "link", "flags": []},
            {"dst": "192.0.2.0/25", "dev": "eth0", "scope": "link", "flags": []},
        ],
        tmp_path,
    )
    lan = hn.decide_route("192.0.2.39")
    assert lan.on_link and lan.arp
    plc = hn.decide_route("203.0.113.10")
    assert plc.on_link and plc.interface.name == "wg0" and not plc.arp
    internet = hn.decide_route("198.51.100.9")
    assert internet.interface.name == "ppp0" and not internet.arp


def test_linux_text_link_flags_mark_tunnels():
    ifaces = linux.parse_addr_text(
        "5: tun0    inet 192.0.2.130/32 scope global tun0\n",
        "5: tun0: <POINTOPOINT,MULTICAST,NOARP,UP,LOWER_UP> mtu 1500 qdisc "
        "fq_codel state UNKNOWN mode DEFAULT group default qlen 500\\    "
        "link/none \n",
    )
    assert not ifaces[0].arp


def test_macos_utun_is_not_an_arp_link():
    hn = HostNetwork(
        interfaces=macos.parse_ifconfig(
            "en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500\n"
            "\tether 00:00:5e:00:53:01\n"
            "\tinet 192.0.2.23 netmask 0xffffff00 broadcast 192.0.2.255\n"
            "\tstatus: active\n"
            "utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1400\n"
        ),
        routes=macos.parse_netstat_routes(
            "Destination        Gateway            Flags        Netif Expire\n"
            "203.0.113.10       utun4              UHS          utun4\n"
        ),
    )
    assert hn.decide_route("192.0.2.39").arp
    tunnel = hn.decide_route("203.0.113.10")
    assert tunnel.on_link and not tunnel.arp


def test_windows_wireguard_and_dialup_vpn_are_not_arp_links():
    hn = _windows(
        adapters=[
            _adapter("イーサネット", 12, "Intel(R) Ethernet Connection I219-LM"),
            _adapter(
                "wg-plant",
                31,
                "WireGuard Tunnel",
                media="IP",
                mac="",
                HardwareInterface=False,
            ),
        ],
        addresses=[
            _address("192.0.2.100", 24, 12, "イーサネット"),
            _address("198.51.100.2", 32, 31, "wg-plant"),
            _address("198.51.100.66", 32, 40, "工場VPN"),
        ],
        routes=[
            _route("192.0.2.0/24", "0.0.0.0", 12, "イーサネット", 256, 25),
            _route("203.0.113.0/24", "0.0.0.0", 31, "wg-plant", 0, 5),
            _route("198.51.100.128/25", "0.0.0.0", 40, "工場VPN", 0, 26),
        ],
    )
    assert hn.decide_route("192.0.2.39").arp
    wg = hn.decide_route("203.0.113.10")
    assert wg.on_link and wg.interface.name == "wg-plant" and not wg.arp
    vpn = hn.decide_route("198.51.100.200")
    assert vpn.on_link and vpn.interface.name == "工場VPN" and not vpn.arp


ROUTE_PRINT_TUNNEL_JA = """\
===========================================================================
インターフェイス一覧
 12...00 00 5e 00 53 0c ......Intel(R) Ethernet Connection I219-LM
 31...........................WireGuard Tunnel
  1...........................Software Loopback Interface 1
===========================================================================

IPv4 ルート テーブル
===========================================================================
アクティブ ルート:
ネットワーク宛先        ネットマスク          ゲートウェイ       インターフェイス  メトリック
          0.0.0.0          0.0.0.0        192.0.2.1      192.0.2.100     25
        192.0.2.0    255.255.255.0            リンク上       192.0.2.100    281
      192.0.2.100  255.255.255.255            リンク上       192.0.2.100    281
      203.0.113.0    255.255.255.0            リンク上      198.51.100.2      5
     198.51.100.2  255.255.255.255            リンク上      198.51.100.2    261
        127.0.0.0        255.0.0.0            リンク上         127.0.0.1    331
===========================================================================
固定ルート:
  なし
"""


def test_windows_route_print_marks_a_host_route_only_link_as_tunnel():
    raw = ROUTE_PRINT_TUNNEL_JA.replace("\n", "\r\n").encode("cp932")
    hn = windows.parse_route_print(decode_output(raw))
    assert hn.decide_route("192.0.2.39").arp
    wg = hn.decide_route("203.0.113.10")
    assert wg.on_link and not wg.arp


def _two_nic_addr():
    return [
        _eth_json("eth0", "192.0.2.20", 24, index=2),
        _eth_json("eth1", "198.51.100.20", 24, index=3),
    ]


def test_multipath_default_route_has_gateways(tmp_path, answered_pings):
    hn = _linux(
        _two_nic_addr(),
        [
            {
                "dst": "default",
                "protocol": "static",
                "metric": 100,
                "flags": [],
                "nexthops": [
                    {"gateway": "192.0.2.1", "dev": "eth0", "weight": 1, "flags": []},
                    {
                        "gateway": "198.51.100.1",
                        "dev": "eth1",
                        "weight": 1,
                        "flags": [],
                    },
                ],
            },
            {"dst": "192.0.2.0/24", "dev": "eth0", "scope": "link", "flags": []},
            {"dst": "198.51.100.0/24", "dev": "eth1", "scope": "link", "flags": []},
        ],
        tmp_path,
    )
    gw = _run(hn, ["gateway"], internet=True)["gateway"]
    assert gw.code == "gateway.ok" and answered_pings == ["192.0.2.1"]
    assert hn.default_gateways() == ["192.0.2.1", "198.51.100.1"]
    assert not hn.decide_route("203.0.113.9").on_link


def test_multipath_default_route_in_text_output():
    routes = linux.parse_route_text(
        "default proto static metric 100 \n"
        "\tnexthop via 192.0.2.1 dev eth0 weight 1 \n"
        "\tnexthop via 198.51.100.1 dev eth1 weight 1 \n"
        "192.0.2.0/24 dev eth0 proto kernel scope link src 192.0.2.20 \n"
    )
    hn = HostNetwork(routes=routes)
    assert hn.default_gateways() == ["192.0.2.1", "198.51.100.1"]
    assert hn.preferred_default_route().metric == 100
    assert {r.destination for r in routes} == {"0.0.0.0/0", "192.0.2.0/24"}


def test_default_route_without_gateway_is_a_link_default(tmp_path):
    hn = _linux(
        [
            _eth_json("eth0", "192.0.2.20", 24, index=2),
            _p2p_json("ppp0", "198.51.100.7", 9, link_type="ppp"),
        ],
        [
            {"dst": "default", "dev": "ppp0", "scope": "link", "flags": []},
            {"dst": "192.0.2.0/24", "dev": "eth0", "scope": "link", "flags": []},
        ],
        tmp_path,
    )
    assert hn.default_routes() == []
    assert hn.link_default_route().interface == "ppp0"
    mobile = _windows(
        adapters=[_adapter("携帯電話", 21, "Mobile Broadband", media="Wireless WAN")],
        addresses=[_address("198.51.100.7", 30, 21, "携帯電話")],
        routes=[_route("0.0.0.0/0", "0.0.0.0", 21, "携帯電話", 0, 40)],
    )
    assert mobile.link_default_route().interface == "携帯電話"
    assert HostNetwork().link_default_route() is None


IP_O_LINK = (
    "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN mode "
    "DEFAULT group default qlen 1000\\    link/loopback 00:00:00:00:00:00 brd "
    "00:00:00:00:00:00\n"
    "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc pfifo_fast state "
    "UP mode DEFAULT group default qlen 1000\\    link/ether 00:00:5e:00:53:02 brd "
    "ff:ff:ff:ff:ff:ff\n"
    "3: docker0: <NO-CARRIER,BROADCAST,MULTICAST,UP> mtu 1500 qdisc noqueue state "
    "DOWN mode DEFAULT group default \\    link/ether 02:42:00:00:53:03 brd "
    "ff:ff:ff:ff:ff:ff\n"
)
IP_O_ADDR = (
    "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
    "2: eth0    inet 192.0.2.20/24 brd 192.0.2.255 scope global eth0\\       "
    "valid_lft forever\n"
    "3: docker0    inet 198.51.100.1/24 scope global docker0\\       "
    "valid_lft forever\n"
)


def test_text_fallback_finds_the_wired_nic(linux_host):
    outputs, sysfs = linux_host
    outputs[("ip", "-o", "link", "show")] = IP_O_LINK
    outputs[("ip", "-o", "addr", "show")] = IP_O_ADDR
    outputs[("ip", "route", "show")] = (
        "default via 192.0.2.1 dev eth0 proto dhcp metric 100\n"
    )
    _sysfs_nic(sysfs, "eth0", {"rx_packets": 10, "tx_packets": 10, "rx_errors": 0})
    (sysfs / "docker0").mkdir()
    hn = linux.collect()
    assert hn.source == "linux:ip-text"
    kinds = {i.name: i.kind for i in hn.interfaces}
    assert kinds == {"lo": "loopback", "eth0": "ethernet", "docker0": "virtual"}
    link = _run(hn, ["link"])["link"]
    assert link.code == "link.ok" and link.summary.startswith("eth0 1 Gbps")


def _proc_hex(ip):
    """How /proc/net prints an IPv4 address: the network-order word in host order."""
    return "%08X" % struct.unpack("=I", socket.inet_aton(ip))[0]


def _proc_route_row(iface, dst, gw, flags, metric, mask):
    return "\t".join(
        [iface, _proc_hex(dst), _proc_hex(gw), f"{flags:04X}", "0", "0"]
        + [str(metric), _proc_hex(mask), "0", "0", "0"]
    )


@pytest.fixture
def no_iproute2(linux_host, monkeypatch, tmp_path):
    outputs, sysfs = linux_host
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(linux, "have", lambda cmd: False)
    monkeypatch.setattr(linux, "PROC_NET", proc)
    addresses = {"lo": ("127.0.0.1", 8), "eth0": ("192.0.2.20", 24)}
    monkeypatch.setattr(
        linux,
        "_ioctl_ipv4",
        lambda name: (linux.Address(*addresses[name]) if name in addresses else None),
    )
    return sysfs, proc


def _sysfs_identity(nic, flags, operstate, arphrd, mac):
    nic.mkdir(parents=True, exist_ok=True)
    (nic / "flags").write_text(f"{flags:#x}\n")
    (nic / "operstate").write_text(f"{operstate}\n")
    (nic / "type").write_text(f"{arphrd}\n")
    (nic / "address").write_text(f"{mac}\n")


def test_without_iproute2_routes_and_interfaces_come_from_proc_and_sys(
    no_iproute2, answered_pings
):
    sysfs, proc = no_iproute2
    _sysfs_nic(sysfs, "eth0", {"rx_packets": 10, "tx_packets": 10, "rx_errors": 0})
    _sysfs_identity(sysfs / "eth0", 0x1003, "up", 1, "00:00:5e:00:53:02")
    _sysfs_identity(sysfs / "lo", 0x9, "unknown", 772, "00:00:00:00:00:00")
    _sysfs_identity(sysfs / "tun0", 0x1091, "unknown", 65534, "")
    (proc / "route").write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU"
        "\tWindow\tIRTT\n"
        + _proc_route_row("eth0", "0.0.0.0", "192.0.2.1", 0x3, 100, "0.0.0.0")
        + "\n"
        + _proc_route_row("eth0", "192.0.2.0", "0.0.0.0", 0x1, 100, "255.255.255.0")
        + "\n"
    )
    hn = linux.collect()
    assert hn.errors == [] and hn.source == "linux:sysfs"
    by = {i.name: i for i in hn.interfaces}
    assert by["eth0"].kind == KIND_ETHERNET and by["eth0"].up
    assert [a.cidr for a in by["eth0"].addresses] == ["192.0.2.20/24"]
    assert by["lo"].kind == "loopback" and not by["tun0"].arp
    res = _run(
        hn,
        ["interface", "gateway", "route"],
        internet=True,
        target={"name": "PLC1", "host": "192.0.2.39"},
    )
    assert res["gateway"].code == "gateway.ok" and answered_pings == ["192.0.2.1"]
    assert res["route"].summary == "on-link via eth0 192.0.2.20/24"


def test_without_iproute2_arp_table_comes_from_proc(no_iproute2):
    _sysfs, proc = no_iproute2
    (proc / "arp").write_text(
        "IP address       HW type     Flags       HW address            Mask     "
        "Device\n"
        "192.0.2.1        0x1         0x2         00:00:5e:00:53:01     *        eth0\n"
        "192.0.2.39       0x1         0x0         00:00:00:00:00:00     *        eth0\n"
    )
    by = {n.ip: n for n in linux.neighbors()}
    assert by["192.0.2.1"].mac == "00:00:5e:00:53:01" and not by["192.0.2.1"].absent
    assert by["192.0.2.39"].absent and by["192.0.2.39"].mac is None
    assert linux.neighbor("192.0.2.1").interface == "eth0"


def test_unreadable_routing_table_is_reported_not_empty(no_iproute2, monkeypatch):
    sysfs, proc = no_iproute2
    monkeypatch.setattr(linux, "SYS_NET", sysfs / "missing")
    hn = linux.collect()
    assert hn.routes == [] and hn.interfaces == []
    assert any("routing table" in e for e in hn.errors)
    assert any("network interfaces" in e for e in hn.errors)


def test_failed_route_commands_on_windows_and_macos_are_reported(monkeypatch):
    hn = windows.parse_collect_json(
        {
            "adapters": [_adapter("イーサネット", 12, "Intel(R) Ethernet")],
            "routes": None,
        }
    )
    assert hn.routes == [] and any("routing table" in e for e in hn.errors)
    assert _two_gateway_laptop().errors == []

    def fake_run(args, timeout=10.0):
        ok = args[0] != "netstat" or "-rn" not in args
        return CommandResult(
            ok=ok, code=0 if ok else 1, stdout="", stderr="", found=True
        )

    monkeypatch.setattr(macos, "run", fake_run)
    monkeypatch.setattr(macos, "have", lambda cmd: False)
    mac = macos.collect()
    assert mac.routes == [] and any("routing table" in e for e in mac.errors)


TAILSCALE_RULES = [
    {"priority": 0, "src": "all", "table": "local"},
    {"priority": 5210, "src": "all", "fwmark": "0x80000", "fwmask": "0xff0000"}
    | {"table": "main"},
    {"priority": 5230, "src": "all", "fwmark": "0x80000", "fwmask": "0xff0000"}
    | {"table": "default"},
    {"priority": 5250, "src": "all", "fwmark": "0x80000", "fwmask": "0xff0000"}
    | {"action": "unreachable"},
    {"priority": 5270, "src": "all", "table": "52"},
    {"priority": 32766, "src": "all", "table": "main"},
    {"priority": 32767, "src": "all", "table": "default"},
]


def _closed_line_pc_with_tailscale(linux_host):
    outputs, sysfs = linux_host
    _sysfs_nic(sysfs, "eth0", {"rx_packets": 10, "tx_packets": 10})
    (sysfs / "tailscale0").mkdir()
    outputs[("ip", "-j", "addr", "show")] = json.dumps(
        [
            _eth_json("eth0", "192.0.2.20", 24, index=2),
            _p2p_json("tailscale0", "198.51.100.16", 5),
        ]
    )
    outputs[("ip", "-j", "-4", "rule", "show")] = json.dumps(TAILSCALE_RULES)
    outputs[("ip", "-j", "-4", "route", "show", "table", "all")] = json.dumps(
        [
            {"dst": "203.0.113.79", "dev": "tailscale0", "table": "52", "flags": []},
            {"dst": "192.0.2.0/24", "dev": "eth0", "scope": "link", "flags": []},
            {
                "type": "local",
                "dst": "192.0.2.20",
                "dev": "eth0",
                "table": "local",
                "scope": "host",
                "flags": [],
            },
        ]
    )
    return linux.collect()


def test_tunnel_routes_outside_the_main_table_are_followed(linux_host):
    hn = _closed_line_pc_with_tailscale(linux_host)
    assert hn.errors == []
    route = _run(hn, ["route"], target={"name": "remote-HMI", "host": "203.0.113.79"})
    assert route["route"].code == "route.on_link"
    assert route["route"].detail["interface"] == "tailscale0"
    assert not hn.decide_route("203.0.113.79").arp
    assert hn.decide_route("192.0.2.39").interface.name == "eth0"
    assert hn.default_routes() == [] and hn.link_default_route() is None


def _wg_quick(extra_rules=(), extra_routes=()):
    hn = HostNetwork(
        interfaces=linux.parse_addr_json(
            [
                _eth_json("eth0", "192.0.2.20", 24, index=2),
                _p2p_json("wg0", "198.51.100.2", 7),
            ]
        ),
        routes=linux.parse_route_json(
            [
                {"dst": "default", "dev": "wg0", "table": "51820", "flags": []},
                {"dst": "default", "gateway": "192.0.2.1", "dev": "eth0"}
                | {"metric": 100, "flags": []},
                {"dst": "192.0.2.0/24", "dev": "eth0", "scope": "link", "flags": []},
                *extra_routes,
            ]
        ),
    )
    hn.rules = linux.parse_rule_json(
        [
            {"priority": 0, "src": "all", "table": "local"},
            {"priority": 32764, "src": "all", "table": "main"}
            | {"suppress_prefixlen": 0},
            {"priority": 32765, "not": None, "src": "all", "fwmark": "0xca6c"}
            | {"table": "51820"},
            {"priority": 32766, "src": "all", "table": "main"},
            {"priority": 32767, "src": "all", "table": "default"},
            *extra_rules,
        ]
    )
    return hn


def test_wg_quick_full_tunnel_sends_internet_traffic_into_the_tunnel():
    hn = _wg_quick()
    internet = hn.decide_route("203.0.113.9")
    assert internet.interface.name == "wg0" and internet.gateway is None
    assert hn.decide_route("192.0.2.39").interface.name == "eth0"
    assert hn.preferred_default_route().gateway == "192.0.2.1"
    assert hn.link_default_route().interface == "wg0"


def test_source_specific_tables_do_not_add_default_gateways():
    hn = _wg_quick(
        extra_rules=[
            {"priority": 100, "src": "192.0.2.20", "table": "100"},
            {"priority": 101, "src": "all", "iif": "eth1", "table": "100"},
        ],
        extra_routes=[
            {"dst": "default", "gateway": "198.51.100.1", "dev": "eth1"}
            | {"table": "100", "flags": []},
        ],
    )
    assert hn.default_gateways() == ["192.0.2.1"]
    assert hn.decide_route("203.0.113.9").interface.name == "wg0"


def test_a_suppressed_main_default_is_no_gateway_to_ping(answered_pings):
    hn = _wg_quick()
    hn.rules = [
        r for r in hn.rules if r.suppress_prefixlength >= 0 or r.table != "main"
    ]
    assert hn.default_routes() == [] and hn.preferred_default_route() is None
    gw = _run(hn, ["gateway"], internet=True)["gateway"]
    assert gw.code == "gateway.none" and answered_pings == []


def _laptop_with_idle_adapters(eth_state="Preferred"):
    return _windows(
        adapters=[
            _adapter("イーサネット", 12, "Intel(R) Ethernet Connection I219-LM"),
            _adapter(
                "Bluetooth ネットワーク接続",
                9,
                "Bluetooth Device (Personal Area Network)",
                status="Disconnected",
            ),
            _adapter(
                "イーサネット 2",
                22,
                "Realtek USB GbE Family Controller",
                status="Disconnected",
            ),
        ],
        addresses=[
            _address("192.0.2.100", 24, 12, "イーサネット", state=eth_state),
            _address(
                "169.254.183.20",
                16,
                9,
                "Bluetooth ネットワーク接続",
                state="Tentative",
                origin="WellKnown",
            ),
            _address(
                "169.254.77.5", 16, 22, "イーサネット 2", "Tentative", "WellKnown"
            ),
        ],
        routes=[
            _route("0.0.0.0/0", "192.0.2.1", 12, "イーサネット", 0, 25),
            _route("192.0.2.0/24", "0.0.0.0", 12, "イーサネット", 256, 25),
        ],
    )


def test_disconnected_adapters_are_not_reported_tentative():
    res = _run(_laptop_with_idle_adapters(), ["interface", "ipconfig"])
    assert res["ipconfig"].code == "ipconfig.ok", res["ipconfig"].summary
    busy = _run(_laptop_with_idle_adapters("Tentative"), ["interface", "ipconfig"])
    assert busy["ipconfig"].summary == "192.0.2.100 on イーサネット is still tentative"


def test_unplugged_nic_gateway_and_dns_are_not_used(monkeypatch):
    hn = _windows(
        adapters=[
            _adapter("イーサネット", 12, "Intel(R) Ethernet", status="Disconnected"),
            _adapter("Wi-Fi", 18, "Intel(R) Wi-Fi 6E AX211", media="Native 802.11"),
        ],
        addresses=[
            _address("192.0.2.100", 24, 12, "イーサネット"),
            _address("198.51.100.40", 24, 18, "Wi-Fi", origin="Dhcp"),
        ],
        routes=[
            _route("0.0.0.0/0", "198.51.100.1", 18, "Wi-Fi", 0, 35),
            _route("0.0.0.0/0", "192.0.2.1", 12, "イーサネット", 0, 5),
            _route("198.51.100.0/24", "0.0.0.0", 18, "Wi-Fi", 256, 35),
        ],
        dns=[
            {"InterfaceIndex": 12, "ServerAddresses": ["192.0.2.53"]},
            {"InterfaceIndex": 18, "ServerAddresses": ["198.51.100.53"]},
        ],
    )

    def wifi_router_only(target, **kw):
        up = target == "198.51.100.1"
        return PingStats(
            target=target,
            address=target,
            sent=4,
            received=4 if up else 0,
            rtts_ms=[1.0] * 4 if up else [],
        )

    monkeypatch.setattr(gateway_probe, "ping", wifi_router_only)
    res = _run(hn, ["interface", "gateway", "ipconfig"], internet=True)
    assert res["gateway"].code == "gateway.ok"
    assert res["gateway"].detail["gateway"] == "198.51.100.1"
    assert res["ipconfig"].code == "ipconfig.ok"
    assert hn.resolvers == ["198.51.100.53"]
    route = hn.decide_route("192.0.2.39")
    assert route.interface.name == "イーサネット" and not route.interface.up
    assert "Get-NetRoute -AddressFamily IPv4 -PolicyStore ActiveStore" in (
        windows.COLLECT_SCRIPT
    )


def test_error_ratio_counts_multicast_frames():
    stats = {
        "Name": "イーサネット",
        "ReceivedUnicastPackets": 120000,
        "ReceivedMulticastPackets": 40000000,
        "ReceivedBroadcastPackets": 300000,
        "SentUnicastPackets": 80000,
        "SentMulticastPackets": 20000000,
        "SentBroadcastPackets": 1000,
        "ReceivedPacketErrors": 25,
        "OutboundPacketErrors": 0,
        "ReceivedDiscardedPackets": 0,
        "OutboundDiscardedPackets": 0,
    }
    hn = _windows(
        adapters=[_adapter("イーサネット", 12, "Intel(R) Ethernet")],
        addresses=[_address("192.0.2.100", 24, 12, "イーサネット")],
        routes=[_route("192.0.2.0/24", "0.0.0.0", 12, "イーサネット", 256, 25)],
        stats=[stats],
    )
    eth = hn.interface("イーサネット").stats
    assert (eth.rx_packets, eth.tx_packets) == (40420000, 20081000)
    assert _run(hn, ["link"])["link"].code == "link.ok"
    for column in ("ReceivedMulticastPackets", "SentBroadcastPackets"):
        assert column in windows.COLLECT_SCRIPT


ARP_INVALID_JA = (
    "\r\nインターフェイス: 192.0.2.100 --- 0xc\r\n"
    "  インターネット アドレス 物理アドレス           種類\r\n"
    "  192.0.2.1             00-00-5e-00-53-01     動的\r\n"
    "  192.0.2.39            00-00-00-00-00-00     無効\r\n"
    "  192.0.2.255           ff-ff-ff-ff-ff-ff     静的\r\n"
).encode("cp932")


def test_invalid_arp_row_is_no_answer_not_a_resolved_neighbour():
    before = {n.ip: n for n in windows.parse_arp_a(decode_output(ARP_INVALID_JA))}
    assert before["192.0.2.1"].mac == "00:00:5e:00:53:01"
    plc = before["192.0.2.39"]
    assert plc.mac is None and plc.absent
    hn = HostNetwork(
        interfaces=windows.parse_route_print(
            "          192.0.2.0    255.255.255.0       On-link     192.0.2.100    281\n"
        ).interfaces
    )
    after = Neighbor(ip="192.0.2.39", mac="00:00:5e:00:53:27", state=NEIGH_STALE)
    res = _run(
        hn,
        ["route", "reach"],
        target={"name": "PLC1", "host": "192.0.2.39"},
        _address="192.0.2.39",
        _ping=PingStats(target="192.0.2.39", sent=10, received=0),
        _ping_window=3.0,
        _neighbor_before=plc,
        _neighbor_after=after,
    )
    assert res["reach"].code == "reach.icmp_blocked"


def test_disabled_duplicate_address_is_not_used_to_reach_devices():
    hn = _windows(
        adapters=[
            _adapter("イーサネット", 12, "Intel(R) Ethernet Connection I219-LM"),
            _adapter("Wi-Fi", 18, "Intel(R) Wi-Fi 6E AX211", media="Native 802.11"),
        ],
        addresses=[
            _address("192.0.2.100", 24, 12, "イーサネット", state="Duplicate"),
            _address("198.51.100.40", 24, 18, "Wi-Fi", origin="Dhcp"),
        ],
        routes=[_route("0.0.0.0/0", "198.51.100.1", 18, "Wi-Fi", 0, 35)],
    )
    res = _run(hn, ["ipconfig", "route"], target={"name": "PLC1", "host": "192.0.2.39"})
    assert res["ipconfig"].code == "ipconfig.duplicate_ip"
    assert res["route"].code != "route.on_link"
    assert res["route"].detail["local_address"] != "192.0.2.100/24"
    assert hn.decide_route("192.0.2.100").special == "self"


def test_resolv_conf_with_a_shift_jis_comment_still_lists_its_servers(
    monkeypatch, tmp_path
):
    conf = tmp_path / "resolv.conf"
    conf.write_bytes(
        "# 工場LAN用 DNS（設定: 生産技術課）\n"
        "nameserver 192.0.2.53\nnameserver 192.0.2.54\n".encode("cp932")
    )
    monkeypatch.setattr(linux, "RESOLV_CONF", conf)
    assert linux.resolvers() == ["192.0.2.53", "192.0.2.54"]


MAC_IFCONFIG = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether 00:00:5e:00:53:0e
\tinet 192.0.2.23 netmask 0xffffff00 broadcast 192.0.2.255
\tmedia: autoselect (1000baseT <full-duplex>)
\tstatus: active
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1400
\tinet 198.51.100.6 --> 198.51.100.5 netmask 0xffffffff
"""

MAC_ROUTES = """\
Routing tables

Internet:
Destination        Gateway            Flags               Netif Expire
0/1                198.51.100.5       UGScg               utun4
default            192.0.2.1          UGScg                 en0
127                127.0.0.1          UCS                   lo0
128.0/1            198.51.100.5       UGSc                utun4
169.254            link#14            UCS                   en0      !
192.0.2            link#14            UCS                   en0      !
192.0.2.1/32       link#14            UCS                   en0      !
198.51.100.5       198.51.100.6       UH                  utun4
203.0.113/24       192.0.2.254        UGSc                  en0
"""


def _mac(routes=MAC_ROUTES):
    return HostNetwork(
        interfaces=macos.parse_ifconfig(MAC_IFCONFIG),
        routes=macos.parse_netstat_routes(routes),
    )


def test_macos_abbreviated_routes_are_followed():
    hn = _mac()
    assert {r.destination for r in hn.routes} >= {
        "0.0.0.0/1",
        "128.0.0.0/1",
        "203.0.113.0/24",
        "169.254.0.0/16",
    }
    line_b = {"name": "PLC-L2", "host": "203.0.113.5"}
    assert _run(hn, ["route"], target=line_b)["route"].summary == (
        "routed via gateway 192.0.2.254"
    )
    no_default_no_vpn = _mac(
        "\n".join(
            line
            for line in MAC_ROUTES.splitlines()
            if "utun4" not in line and not line.startswith("default")
        )
    )
    assert not any(r.is_default for r in no_default_no_vpn.routes)
    assert (
        _run(no_default_no_vpn, ["route"], target=line_b)["route"].code
        == "route.via_gateway"
    )
    assert hn.decide_route("198.51.100.77").gateway == "198.51.100.5"


def test_macos_utun_address_and_inactive_counters_are_read():
    utun = _mac().interface("utun4")
    assert [a.cidr for a in utun.addresses] == ["198.51.100.6/32"] and not utun.arp
    stats = macos.parse_netstat_ibn(
        "Name       Mtu   Network       Address            Ipkts Ierrs     Ibytes"
        "    Opkts Oerrs     Obytes  Coll\n"
        "en7*       1500  <Link#17>   00:00:5e:00:53:11    54321    37    6543210"
        "    43210     0    5432100     0\n"
    )
    assert stats["en7"].rx_errors == 37
