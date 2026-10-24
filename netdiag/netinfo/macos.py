"""macOS collector: ``ifconfig``, ``netstat`` and ``arp`` (C locale).

``ifconfig`` carries the negotiated media (``1000baseT <full-duplex>``), which is
where macOS reports speed and duplex; ``netstat -ibn`` provides error counters.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from ..core.util import have, run
from .model import (
    KIND_ETHERNET,
    KIND_LOOPBACK,
    KIND_OTHER,
    KIND_VIRTUAL,
    KIND_WIFI,
    NEIGH_INCOMPLETE,
    NEIGH_PERMANENT,
    NEIGH_STALE,
    Address,
    HostNetwork,
    Interface,
    LinkStats,
    Neighbor,
    Route,
    normalize_mac,
)

_IF_HEADER_RE = re.compile(r"^(\S+?):\s+flags=\w+<([^>]*)>(?:.*?\bmtu\s+(\d+))?")
_INET_RE = re.compile(
    r"^\s+inet\s+(\d+\.\d+\.\d+\.\d+)\s+netmask\s+(0x[0-9a-fA-F]+|\S+)"
)
_MEDIA_RE = re.compile(r"^\s+media:.*?\(([^)]*)\)")
_SPEED_RE = re.compile(r"(\d+)(G?)base", re.IGNORECASE)


def _prefix_from_mask(mask: str) -> int:
    if mask.startswith("0x"):
        return bin(int(mask, 16)).count("1")
    return sum(bin(int(o)).count("1") for o in mask.split("."))


def parse_ifconfig(
    text: str, wifi_devices: Optional[List[str]] = None
) -> List[Interface]:
    wifi = set(wifi_devices or [])
    out: List[Interface] = []
    cur: Optional[Interface] = None
    for line in text.splitlines():
        m = _IF_HEADER_RE.match(line)
        if m:
            name, flags, mtu = m.group(1), m.group(2).split(","), m.group(3)
            cur = Interface(
                name=name, admin_up="UP" in flags, mtu=int(mtu) if mtu else None
            )
            cur.up = "UP" in flags and "RUNNING" in flags
            if "LOOPBACK" in flags:
                cur.kind = KIND_LOOPBACK
            elif name in wifi:
                cur.kind = KIND_WIFI
            elif re.match(
                r"^(utun|bridge|awdl|llw|gif|stf|anpi|ap|vmnet|vboxnet)", name
            ):
                cur.kind = KIND_VIRTUAL
            elif name.startswith("en"):
                cur.kind = KIND_ETHERNET
            else:
                cur.kind = KIND_OTHER
            out.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("ether "):
            cur.mac = normalize_mac(s.split()[1])
            continue
        m = _INET_RE.match(line)
        if m:
            cur.addresses.append(
                Address(ip=m.group(1), prefixlen=_prefix_from_mask(m.group(2)))
            )
            continue
        m = _MEDIA_RE.match(line)
        if m:
            media = m.group(1)
            sp = _SPEED_RE.search(media)
            if sp:
                cur.speed_mbps = float(sp.group(1)) * (1000 if sp.group(2) else 1)
            if "full-duplex" in media:
                cur.duplex = "full"
            elif "half-duplex" in media:
                cur.duplex = "half"
            continue
        if s.startswith("status:"):
            cur.up = cur.up and s.split(":", 1)[1].strip() == "active"
    return out


def parse_netstat_routes(text: str) -> List[Route]:
    routes: List[Route] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0] in ("Destination", "Routing", "Internet:"):
            continue
        dst, gw, _flags, netif = parts[0], parts[1], parts[2], parts[3]
        if dst == "default":
            dst = "0.0.0.0/0"
        elif "/" not in dst:
            octets = dst.split(".")
            if not all(o.isdigit() for o in octets):
                continue
            plen = 8 * len(octets)
            dst = ".".join(octets + ["0"] * (4 - len(octets))) + f"/{plen}"
        is_ip = re.fullmatch(r"\d+\.\d+\.\d+\.\d+", gw) is not None
        routes.append(
            Route(destination=dst, gateway=gw if is_ip else None, interface=netif)
        )
    return routes


_ARP_RE = re.compile(r"\((\d+\.\d+\.\d+\.\d+)\)\s+at\s+(\S+)(?:\s+on\s+(\S+))?")


def parse_arp_an(text: str) -> List[Neighbor]:
    out: List[Neighbor] = []
    for line in text.splitlines():
        m = _ARP_RE.search(line)
        if not m:
            continue
        ip, mac_s, iface = m.groups()
        mac = normalize_mac(mac_s)
        if mac is None:
            state = NEIGH_INCOMPLETE
        elif "permanent" in line:
            state = NEIGH_PERMANENT
        else:
            state = NEIGH_STALE  # macOS does not expose freshness
        out.append(Neighbor(ip=ip, mac=mac, state=state, interface=iface))
    return out


def parse_scutil_dns(text: str) -> List[str]:
    servers: List[str] = []
    for m in re.finditer(r"nameserver\[\d+\]\s*:\s*(\S+)", text):
        if m.group(1) not in servers:
            servers.append(m.group(1))
    return servers


def parse_netstat_ibn(text: str) -> Dict[str, LinkStats]:
    """Link-level rows of ``netstat -ibn`` (``<Link#N>``) -> counters."""
    out: Dict[str, LinkStats] = {}
    header: List[str] = []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "Name":
            header = parts
            continue
        if not header or len(parts) < 3 or not parts[2].startswith("<Link"):
            continue
        # Rows may omit the Address column; align from the right.
        values = dict(zip(reversed(header), reversed(parts)))

        def _i(key: str) -> Optional[int]:
            try:
                return int(values[key])
            except (KeyError, ValueError):
                return None

        out.setdefault(
            parts[0],
            LinkStats(
                rx_packets=_i("Ipkts"),
                tx_packets=_i("Opkts"),
                rx_errors=_i("Ierrs"),
                tx_errors=_i("Oerrs"),
                collisions=_i("Coll"),
            ),
        )
    return out


def _wifi_devices() -> List[str]:
    if not have("networksetup"):
        return []
    text = run(["networksetup", "-listallhardwareports"], timeout=8).stdout
    devs: List[str] = []
    port = ""
    for line in text.splitlines():
        if line.startswith("Hardware Port:"):
            port = line.split(":", 1)[1].strip()
        elif line.startswith("Device:") and port in ("Wi-Fi", "AirPort"):
            devs.append(line.split(":", 1)[1].strip())
    return devs


def collect() -> HostNetwork:
    hn = HostNetwork(source="macos")
    hn.interfaces = parse_ifconfig(
        run(["ifconfig", "-a"], timeout=8).stdout, _wifi_devices()
    )
    stats = parse_netstat_ibn(run(["netstat", "-ibn"], timeout=8).stdout)
    for iface in hn.interfaces:
        iface.stats = stats.get(iface.name)
    hn.routes = parse_netstat_routes(
        run(["netstat", "-rn", "-f", "inet"], timeout=8).stdout
    )
    hn.resolvers = parse_scutil_dns(run(["scutil", "--dns"], timeout=8).stdout)
    return hn


def neighbors() -> List[Neighbor]:
    return parse_arp_an(run(["arp", "-an"], timeout=8).stdout)


def neighbor(ip: str) -> Optional[Neighbor]:
    res = run(["arp", "-n", ip], timeout=8)
    for n in parse_arp_an(res.stdout):
        if n.ip == ip:
            return n
    return Neighbor(ip=ip, state=NEIGH_INCOMPLETE) if res.found else None


def link_stats() -> Dict[str, LinkStats]:
    return parse_netstat_ibn(run(["netstat", "-ibn"], timeout=8).stdout)
