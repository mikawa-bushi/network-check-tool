"""OS collectors: parsing real-format output into the shared network model.

Fixtures follow the verified output formats of iproute2 (``ip -j``), Windows
PowerShell (``ConvertTo-Json``), Japanese/English ``arp -a`` and ``route print``,
and macOS ``ifconfig``/``netstat``/``arp``/``scutil`` — no real network needed.
"""

import json

from netdiag.netinfo import linux, macos, windows
from netdiag.netinfo.model import (
    KIND_ETHERNET,
    KIND_LOOPBACK,
    KIND_VIRTUAL,
    KIND_WIFI,
    Address,
    HostNetwork,
    Interface,
    LinkStats,
    Route,
    mac_is_local,
    normalize_mac,
)

# ----------------------------------------------------------------- model helpers


def test_normalize_mac_variants():
    assert normalize_mac("00-11-22-AA-BB-CC") == "00:11:22:aa:bb:cc"
    assert normalize_mac("0:1b:21:a:b:c") == "00:1b:21:0a:0b:0c"  # macOS drops zeros
    assert normalize_mac("001122AABBCC") == "00:11:22:aa:bb:cc"
    assert normalize_mac("(incomplete)") is None
    assert normalize_mac("") is None
    assert mac_is_local("02:00:00:00:00:01") and not mac_is_local("00:11:22:33:44:55")


def test_link_stats_delta():
    before = LinkStats(rx_packets=100, rx_errors=1, rx_crc_errors=0)
    after = LinkStats(rx_packets=150, rx_errors=4, rx_crc_errors=2)
    d = after.delta(before)
    assert (d.rx_packets, d.rx_errors, d.rx_crc_errors, d.tx_errors) == (50, 3, 2, None)
    assert d.errors_total() == 3  # the 2 CRC errors are part of rx_errors


# ------------------------------------------------------------------- route model


def _net():
    eth = Interface(
        name="イーサネット 2",
        kind=KIND_ETHERNET,
        up=True,
        addresses=[Address("192.168.3.100", 24)],
    )
    wifi = Interface(
        name="Wi-Fi", kind=KIND_WIFI, up=True, addresses=[Address("10.1.2.3", 16)]
    )
    down = Interface(
        name="eth9", kind=KIND_ETHERNET, up=False, addresses=[Address("172.20.0.5", 24)]
    )
    return HostNetwork(
        interfaces=[eth, wifi, down],
        routes=[Route("0.0.0.0/0", gateway="10.1.0.1", interface="Wi-Fi", metric=35)],
    )


def test_decide_route_on_link_gateway_and_specials():
    hn = _net()
    d = hn.decide_route("192.168.3.39")
    assert d.on_link and d.interface.name == "イーサネット 2" and d.gateway is None
    d = hn.decide_route("8.8.8.8")
    assert not d.on_link and d.gateway == "10.1.0.1"
    assert hn.decide_route("192.168.3.100").special == "self"
    assert hn.decide_route("192.168.3.0").special == "network"
    assert hn.decide_route("192.168.3.255").special == "broadcast"
    assert hn.decide_route("127.0.0.1").special == "loopback"
    assert hn.decide_route("not-an-ip").special == "invalid"
    d = hn.decide_route("172.20.0.9")
    assert d.on_link and not d.interface.up  # the route probe reports link down


def test_decide_route_without_gateway_is_no_route():
    hn = _net()
    hn.routes = []
    d = hn.decide_route("192.168.10.5")
    assert not d.on_link and d.gateway is None and d.interface is None


def test_decide_route_overlap_lists_all_candidates():
    hn = _net()
    hn.interfaces.append(
        Interface(
            name="USB-LAN",
            kind=KIND_ETHERNET,
            up=True,
            addresses=[Address("192.168.3.200", 24)],
        )
    )
    d = hn.decide_route("192.168.3.39")
    assert len(d.candidates) == 2


# --------------------------------------------------------------------- linux

IP_ADDR_JSON = [
    {
        "ifindex": 1,
        "ifname": "lo",
        "flags": ["LOOPBACK", "UP", "LOWER_UP"],
        "mtu": 65536,
        "operstate": "UNKNOWN",
        "link_type": "loopback",
        "address": "00:00:00:00:00:00",
        "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}],
    },
    {
        "ifindex": 2,
        "ifname": "enp3s0",
        "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"],
        "mtu": 1500,
        "operstate": "UP",
        "link_type": "ether",
        "address": "00:0c:29:02:c8:55",
        "addr_info": [
            {
                "family": "inet",
                "local": "192.168.3.100",
                "prefixlen": 24,
                "dynamic": True,
            },
            {"family": "inet6", "local": "fe80::1", "prefixlen": 64, "dadfailed": True},
        ],
    },
    {
        "ifindex": 3,
        "ifname": "wlan0",
        "flags": ["BROADCAST", "MULTICAST", "UP"],
        "mtu": 1500,
        "operstate": "DOWN",
        "link_type": "ether",
        "address": "aa:bb:cc:dd:ee:ff",
        "addr_info": [],
    },
    {
        "ifindex": 4,
        "ifname": "docker0",
        "flags": ["NO-CARRIER", "BROADCAST", "MULTICAST", "UP"],
        "operstate": "DOWN",
        "link_type": "ether",
        "address": "02:42:ac:11:00:01",
        "addr_info": [{"family": "inet", "local": "172.17.0.1", "prefixlen": 16}],
    },
]


def _sysfs(tmp_path):
    base = tmp_path / "net"
    eth = base / "enp3s0"
    (eth / "device").mkdir(parents=True)
    (eth / "statistics").mkdir()
    (eth / "carrier").write_text("1\n")
    (eth / "speed").write_text("100\n")
    (eth / "duplex").write_text("half\n")
    for name, value in {
        "rx_packets": 1000,
        "tx_packets": 900,
        "rx_errors": 7,
        "tx_errors": 0,
        "rx_dropped": 0,
        "tx_dropped": 0,
        "rx_crc_errors": 7,
        "collisions": 3,
    }.items():
        (eth / "statistics" / name).write_text(f"{value}\n")
    (base / "wlan0" / "wireless").mkdir(parents=True)
    (base / "wlan0" / "device").mkdir()
    (base / "docker0").mkdir()
    return base


def test_linux_parse_addr_json_with_sysfs(tmp_path):
    base = _sysfs(tmp_path)
    ifaces = {i.name: i for i in linux.parse_addr_json(IP_ADDR_JSON, base=base)}
    assert ifaces["lo"].kind == KIND_LOOPBACK
    eth = ifaces["enp3s0"]
    assert eth.kind == KIND_ETHERNET and eth.up
    assert (eth.speed_mbps, eth.duplex) == (100.0, "half")
    assert eth.addresses[0].flags == ["dhcp"]
    assert eth.addresses[1].flags == ["duplicate"]
    assert eth.stats.rx_crc_errors == 7 and eth.stats.collisions == 3
    assert ifaces["wlan0"].kind == KIND_WIFI and not ifaces["wlan0"].up
    assert ifaces["docker0"].kind == KIND_VIRTUAL and not ifaces["docker0"].up


def test_linux_parse_routes_and_neighbours():
    routes = linux.parse_route_json(
        [
            {
                "dst": "default",
                "gateway": "192.168.3.1",
                "dev": "enp3s0",
                "metric": 100,
            },
            {"dst": "192.168.3.0/24", "dev": "enp3s0", "scope": "link"},
            {"dst": "10.9.9.9", "gateway": "192.168.3.254", "dev": "enp3s0"},
        ]
    )
    assert routes[0].is_default and routes[0].gateway == "192.168.3.1"
    assert routes[2].destination == "10.9.9.9/32"
    neigh = linux.parse_neigh_json(
        [
            {
                "dst": "192.168.3.39",
                "dev": "enp3s0",
                "lladdr": "00:26:92:AA:BB:CC",
                "state": ["REACHABLE"],
                "confirmed": 3,
            },
            {"dst": "192.168.3.77", "dev": "enp3s0", "state": ["FAILED"]},
            {
                "dst": "fe80::1",
                "dev": "enp3s0",
                "lladdr": "00:11:22:33:44:55",
                "state": ["STALE"],
            },
        ]
    )
    assert len(neigh) == 2  # IPv6 entries ignored
    assert (
        neigh[0].mac == "00:26:92:aa:bb:cc"
        and neigh[0].confirmed
        and neigh[0].confirmed_age == 3.0
    )
    assert neigh[1].absent and neigh[1].mac is None


def test_linux_text_fallbacks():
    ifaces = linux.parse_addr_text(
        "2: eth0    inet 192.168.1.10/24 brd 192.168.1.255 scope global eth0\n",
        "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP link/ether 00:11:22:33:44:55 brd ff:ff:ff:ff:ff:ff\n",
    )
    assert (
        ifaces[0].name == "eth0"
        and ifaces[0].up
        and ifaces[0].mac == "00:11:22:33:44:55"
    )
    routes = linux.parse_route_text(
        "default via 192.168.1.1 dev eth0 proto dhcp metric 100\n"
    )
    assert routes[0].gateway == "192.168.1.1" and routes[0].metric == 100
    neigh = linux.parse_neigh_text(
        "192.168.1.1 dev eth0 lladdr aa:bb:cc:dd:ee:ff REACHABLE\n"
    )
    assert neigh[0].mac == "aa:bb:cc:dd:ee:ff" and neigh[0].confirmed


def test_linux_resolv_conf():
    assert linux.parse_resolv_conf(
        "# c\nnameserver 10.0.0.53\nnameserver 10.0.0.54\nsearch x\n"
    ) == [
        "10.0.0.53",
        "10.0.0.54",
    ]


# ------------------------------------------------------------------- windows

WIN_JSON = {
    "adapters": [
        {
            "Name": "イーサネット 2",
            "InterfaceDescription": "Intel(R) Ethernet Connection I219-LM",
            "ifIndex": 12,
            "Status": "Up",
            "MacAddress": "00-26-92-AA-BB-CC",
            "LinkSpeed": "100 Mbps",
            "ReceiveLinkSpeed": 100000000,
            "FullDuplex": False,
            "MtuSize": 1500,
            "Virtual": False,
            "HardwareInterface": True,
            "MediaType": "802.3",
            "PhysicalMediaType": "802.3",
        },
        {
            "Name": "Wi-Fi",
            "InterfaceDescription": "Intel(R) Wi-Fi 6 AX201 160MHz",
            "ifIndex": 18,
            "Status": "Up",
            "MacAddress": "AA-BB-CC-DD-EE-01",
            "LinkSpeed": "866.7 Mbps",
            "ReceiveLinkSpeed": 866700000,
            "FullDuplex": True,
            "Virtual": False,
            "HardwareInterface": True,
            "MediaType": "Native 802.11",
            "PhysicalMediaType": "Native 802.11",
        },
        {
            "Name": "vEthernet (Default Switch)",
            "InterfaceDescription": "Hyper-V Virtual Ethernet Adapter",
            "ifIndex": 25,
            "Status": "Up",
            "MacAddress": "00-15-5D-01-02-03",
            "Virtual": True,
            "HardwareInterface": False,
            "MediaType": "802.3",
        },
    ],
    # one-element result: ConvertTo-Json may emit a bare object instead of a list
    "addresses": [
        {
            "IPAddress": "192.168.3.100",
            "PrefixLength": 24,
            "InterfaceIndex": 12,
            "InterfaceAlias": "イーサネット 2",
            "AddressState": "Duplicate",
            "PrefixOrigin": "Manual",
        },
        {
            "IPAddress": "10.1.2.3",
            "PrefixLength": 16,
            "InterfaceIndex": 18,
            "InterfaceAlias": "Wi-Fi",
            "AddressState": "4",
            "PrefixOrigin": "3",
        },
        {
            "IPAddress": "127.0.0.1",
            "PrefixLength": 8,
            "InterfaceIndex": 1,
            "InterfaceAlias": "Loopback Pseudo-Interface 1",
            "AddressState": "Preferred",
            "PrefixOrigin": "WellKnown",
        },
    ],
    "routes": [
        {
            "DestinationPrefix": "0.0.0.0/0",
            "NextHop": "10.1.0.1",
            "InterfaceIndex": 18,
            "RouteMetric": 0,
            "InterfaceMetric": 35,
        },
        {
            "DestinationPrefix": "192.168.3.0/24",
            "NextHop": "0.0.0.0",
            "InterfaceIndex": 12,
            "RouteMetric": 256,
            "InterfaceMetric": 25,
        },
    ],
    "dns": {"InterfaceIndex": 18, "ServerAddresses": ["10.1.0.53", "10.1.0.54"]},
    "stats": {
        "Name": "イーサネット 2",
        "ReceivedUnicastPackets": 5000,
        "SentUnicastPackets": 4000,
        "ReceivedPacketErrors": 12,
        "OutboundPacketErrors": 0,
        "ReceivedDiscardedPackets": 1,
        "OutboundDiscardedPackets": 0,
    },
    "firewall": [
        {
            "Name": "Domain",
            "Enabled": "True",
            "DefaultInboundAction": "NotConfigured",
            "DefaultOutboundAction": "NotConfigured",
        }
    ],
}


def test_windows_powershell_json():
    hn = windows.parse_collect_json(
        json.loads(json.dumps(WIN_JSON, ensure_ascii=False))
    )
    by = {i.name: i for i in hn.interfaces}
    eth = by["イーサネット 2"]
    assert eth.kind == KIND_ETHERNET and eth.up
    assert (eth.speed_mbps, eth.duplex) == (100.0, "half")
    assert eth.mac == "00:26:92:aa:bb:cc"
    assert eth.addresses[0].flags == [
        "duplicate",
        "manual",
    ]  # Windows' own IP-conflict detection
    assert eth.stats.rx_errors == 12
    wifi = by["Wi-Fi"]
    assert wifi.kind == KIND_WIFI and wifi.addresses[0].flags == [
        "dhcp"
    ]  # enum integers mapped
    assert by["vEthernet (Default Switch)"].kind == KIND_VIRTUAL
    assert hn.default_gateways() == ["10.1.0.1"]
    assert hn.routes[0].metric == 35  # route + interface metric
    assert hn.routes[1].gateway is None  # 0.0.0.0 next hop = on-link
    assert hn.resolvers == ["10.1.0.53", "10.1.0.54"]
    assert hn.extras["firewall"][0]["Name"] == "Domain"


def test_windows_link_speed_strings():
    assert windows.parse_link_speed("1 Gbps") == 1000.0
    assert windows.parse_link_speed("866.7 Mbps") == 866.7
    assert windows.parse_link_speed(10000000) == 10.0
    assert windows.parse_link_speed("0 bps") == 0.0
    assert windows.parse_link_speed(None) is None


ARP_JA = """
インターフェイス: 192.168.1.14 --- 0xd
  インターネット アドレス 物理アドレス           種類
  192.168.1.1           cc-1a-fa-c0-a0-6e     動的
  192.168.1.2           a8-13-74-58-75-37     動的
  224.0.0.22            01-00-5e-00-00-16     静的
  255.255.255.255       ff-ff-ff-ff-ff-ff     静的
"""

ARP_EN = """
Interface: 192.168.1.3 --- 0xb
  Internet Address      Physical Address      Type
  192.168.1.2           00-50-56-be-f6-db     dynamic
"""


def test_windows_arp_a_any_language():
    ja = windows.parse_arp_a(ARP_JA)
    assert [(n.ip, n.mac, n.interface) for n in ja] == [
        ("192.168.1.1", "cc:1a:fa:c0:a0:6e", "192.168.1.14"),
        ("192.168.1.2", "a8:13:74:58:75:37", "192.168.1.14"),
    ]
    en = windows.parse_arp_a(ARP_EN)
    assert en[0].mac == "00:50:56:be:f6:db"
    assert windows.parse_arp_a("ARP エントリが見つかりませんでした。\n") == []


ROUTE_PRINT_JA = """
===========================================================================
インターフェイス一覧
 12...00 26 92 aa bb cc ......Intel(R) Ethernet Connection
===========================================================================

IPv4 ルート テーブル
===========================================================================
アクティブ ルート:
ネットワーク宛先        ネットマスク          ゲートウェイ       インターフェイス  メトリック
          0.0.0.0          0.0.0.0      192.168.0.1    192.168.0.103     35
        127.0.0.0        255.0.0.0            リンク上         127.0.0.1    331
      192.168.0.0    255.255.255.0            リンク上     192.168.0.103    291
    192.168.0.103  255.255.255.255            リンク上     192.168.0.103    291
===========================================================================
固定ルート:
  ネットワーク アドレス          ネットマスク  ゲートウェイ アドレス  メトリック
          0.0.0.0          0.0.0.0      192.168.0.1  既定
===========================================================================
"""


def test_windows_route_print_fallback():
    hn = windows.parse_route_print(ROUTE_PRINT_JA)
    assert hn.default_gateways() == ["192.168.0.1"]
    assert len([r for r in hn.routes if r.is_default]) == 1  # persistent row ignored
    iface = hn.interfaces[0]
    assert iface.addresses[0].cidr == "192.168.0.103/24"
    assert hn.decide_route("192.168.0.50").on_link


def test_windows_neighbors_json_enum_integers():
    neigh = windows.parse_neighbors_json(
        {
            "IPAddress": "192.168.3.39",
            "LinkLayerAddress": "00-00-00-00-00-00",
            "State": 0,
        }
    )
    assert neigh[0].mac is None and neigh[0].absent


# --------------------------------------------------------------------- macOS

IFCONFIG = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether 0:1b:21:a:b:c
\tinet 192.168.3.100 netmask 0xffffff00 broadcast 192.168.3.255
\tmedia: autoselect (1000baseT <full-duplex,flow-control>)
\tstatus: active
en1: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether aa:bb:cc:dd:ee:01
\tinet 10.1.2.3 netmask 0xffff0000 broadcast 10.1.255.255
\tmedia: autoselect
\tstatus: active
utun0: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380
"""


def test_macos_ifconfig():
    ifaces = {i.name: i for i in macos.parse_ifconfig(IFCONFIG, wifi_devices=["en1"])}
    en0 = ifaces["en0"]
    assert en0.kind == KIND_ETHERNET and en0.up
    assert (en0.speed_mbps, en0.duplex) == (1000.0, "full")
    assert en0.mac == "00:1b:21:0a:0b:0c"
    assert en0.addresses[0].cidr == "192.168.3.100/24"
    assert (
        ifaces["en1"].kind == KIND_WIFI and ifaces["en1"].addresses[0].prefixlen == 16
    )
    assert ifaces["lo0"].kind == KIND_LOOPBACK
    assert ifaces["utun0"].kind == KIND_VIRTUAL


def test_macos_routes_arp_dns_stats():
    routes = macos.parse_netstat_routes(
        "Routing tables\n\nInternet:\nDestination        Gateway            Flags               Netif Expire\n"
        "default            192.168.8.1        UGScg                 en0\n"
        "192.168.8         link#6             UCS                   en0      !\n"
    )
    assert routes[0].is_default and routes[0].gateway == "192.168.8.1"
    assert routes[1].destination == "192.168.8.0/24" and routes[1].gateway is None
    neigh = macos.parse_arp_an(
        "? (192.168.1.1) at 0:11:22:aa:bb:cc on en0 ifscope [ethernet]\n"
        "? (192.168.1.77) at (incomplete) on en0 ifscope [ethernet]\n"
    )
    assert neigh[0].mac == "00:11:22:aa:bb:cc"
    assert neigh[1].mac is None and neigh[1].absent
    assert macos.parse_scutil_dns(
        "resolver #1\n  nameserver[0] : 192.168.1.1\n  nameserver[1] : 192.168.1.2\n"
    ) == [
        "192.168.1.1",
        "192.168.1.2",
    ]
    stats = macos.parse_netstat_ibn(
        "Name       Mtu   Network       Address            Ipkts Ierrs     Ibytes    Opkts Oerrs     Obytes  Coll\n"
        "en0        1500  <Link#6>      00:1b:21:0a:0b:0c  12345     3    9999999    23456     0    8888888     0\n"
        "en0        1500  192.168.3     192.168.3.100      12000     -    9000000    23000     -    8000000     -\n"
    )
    assert stats["en0"].rx_errors == 3 and stats["en0"].rx_packets == 12345
