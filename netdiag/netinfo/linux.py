"""Linux collector: iproute2 JSON output (``ip -j``) plus sysfs link details.

``ip -j`` gives structured data that does not depend on column layout or
locale; sysfs adds what ``ip`` does not show (negotiated speed/duplex, error
counters, whether a NIC is physical, Wi-Fi or virtual). A plain-text fallback
covers old/BusyBox ``ip`` builds without ``-j``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from ..core.util import have, run
from .model import (
    KIND_ETHERNET,
    KIND_LOOPBACK,
    KIND_OTHER,
    KIND_VIRTUAL,
    KIND_WIFI,
    NEIGH_DELAY,
    NEIGH_FAILED,
    NEIGH_INCOMPLETE,
    NEIGH_PERMANENT,
    NEIGH_PROBE,
    NEIGH_REACHABLE,
    NEIGH_STALE,
    NEIGH_UNKNOWN,
    Address,
    HostNetwork,
    Interface,
    LinkStats,
    Neighbor,
    Route,
    normalize_mac,
)

SYS_NET = Path("/sys/class/net")

_NEIGH_STATES = {
    "REACHABLE": NEIGH_REACHABLE,
    "PERMANENT": NEIGH_PERMANENT,
    "NOARP": NEIGH_PERMANENT,
    "STALE": NEIGH_STALE,
    "DELAY": NEIGH_DELAY,
    "PROBE": NEIGH_PROBE,
    "INCOMPLETE": NEIGH_INCOMPLETE,
    "FAILED": NEIGH_FAILED,
}


def _ip_json(*args: str) -> Optional[list]:
    res = run(["ip", "-j", *args], timeout=8)
    if not res.ok or not res.stdout.strip().startswith("["):
        return None
    try:
        data = json.loads(res.stdout)
    except ValueError:
        return None
    return data if isinstance(data, list) else None


# --------------------------------------------------------------------------- sysfs


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None


def _read_int(path: Path) -> Optional[int]:
    v = _read(path)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def read_link_stats(name: str, base: Path = SYS_NET) -> Optional[LinkStats]:
    st = base / name / "statistics"
    if not st.is_dir():
        return None
    return LinkStats(
        rx_packets=_read_int(st / "rx_packets"),
        tx_packets=_read_int(st / "tx_packets"),
        rx_errors=_read_int(st / "rx_errors"),
        tx_errors=_read_int(st / "tx_errors"),
        rx_dropped=_read_int(st / "rx_dropped"),
        tx_dropped=_read_int(st / "tx_dropped"),
        rx_crc_errors=_read_int(st / "rx_crc_errors"),
        collisions=_read_int(st / "collisions"),
        carrier_changes=_read_int(base / name / "carrier_changes"),
    )


def _kind(name: str, link_type: Optional[str], flags: List[str], base: Path) -> str:
    if "LOOPBACK" in flags or link_type == "loopback":
        return KIND_LOOPBACK
    d = base / name
    if (d / "wireless").exists() or (d / "phy80211").exists():
        return KIND_WIFI
    if (d / "device").exists():
        return KIND_ETHERNET if link_type in (None, "ether") else KIND_OTHER
    if d.exists() or link_type in ("none", "ether"):
        return KIND_VIRTUAL  # bridge, veth, docker, tun/VPN ...
    return KIND_OTHER


def _apply_sysfs(iface: Interface, base: Path = SYS_NET) -> None:
    d = base / iface.name
    if not d.exists():
        return
    carrier = _read_int(d / "carrier")
    if carrier is not None:
        iface.up = carrier == 1 and iface.up
    speed = _read_int(d / "speed")
    if speed is not None and speed > 0:
        iface.speed_mbps = float(speed)
    duplex = _read(d / "duplex")
    if duplex in ("full", "half"):
        iface.duplex = duplex
    iface.stats = read_link_stats(iface.name, base)


# --------------------------------------------------------------------------- parse


def parse_addr_json(data: list, base: Path = SYS_NET) -> List[Interface]:
    out: List[Interface] = []
    for rec in data:
        name = rec.get("ifname")
        if not name:
            continue
        flags = [str(f) for f in rec.get("flags", [])]
        oper = str(rec.get("operstate", "")).upper()
        iface = Interface(
            name=name,
            index=rec.get("ifindex"),
            mac=normalize_mac(rec.get("address")),
            admin_up="UP" in flags,
            # UNKNOWN is what loopback/tun report while working; trust LOWER_UP.
            up="LOWER_UP" in flags and oper in ("UP", "UNKNOWN"),
            mtu=rec.get("mtu"),
        )
        iface.kind = _kind(name, rec.get("link_type"), flags, base)
        for a in rec.get("addr_info", []):
            fam = a.get("family")
            if fam not in ("inet", "inet6") or not a.get("local"):
                continue
            aflags = []
            if a.get("dynamic"):
                aflags.append("dhcp")
            if a.get("dadfailed"):
                aflags.append("duplicate")
            if a.get("tentative"):
                aflags.append("tentative")
            iface.addresses.append(
                Address(
                    ip=a["local"],
                    prefixlen=int(a.get("prefixlen", 32)),
                    family=4 if fam == "inet" else 6,
                    flags=aflags,
                )
            )
        _apply_sysfs(iface, base)
        out.append(iface)
    return out


def parse_route_json(data: list) -> List[Route]:
    routes: List[Route] = []
    for rec in data:
        dst = rec.get("dst")
        if not dst:
            continue
        if dst == "default":
            dst = "0.0.0.0/0"
        elif "/" not in dst and ":" not in dst:
            dst = f"{dst}/32"
        if ":" in dst:
            continue  # IPv4 table only
        routes.append(
            Route(
                destination=dst,
                gateway=rec.get("gateway"),
                interface=rec.get("dev"),
                metric=rec.get("metric"),
            )
        )
    return routes


def parse_neigh_json(data: list) -> List[Neighbor]:
    out: List[Neighbor] = []
    for rec in data:
        ip = rec.get("dst")
        if not ip or ":" in ip:
            continue
        states = rec.get("state") or []
        if isinstance(states, str):
            states = [states]
        state = NEIGH_UNKNOWN
        for s in states:
            state = _NEIGH_STATES.get(str(s).upper(), state)
        confirmed = rec.get("confirmed")
        out.append(
            Neighbor(
                ip=ip,
                mac=normalize_mac(rec.get("lladdr")),
                state=state,
                interface=rec.get("dev"),
                confirmed_age=(
                    float(confirmed) if isinstance(confirmed, (int, float)) else None
                ),
            )
        )
    return out


_ADDR_TEXT_RE = re.compile(r"^\d+:\s+(\S+)\s+(inet6?)\s+(\S+)")


def parse_addr_text(addr_text: str, link_text: str = "") -> List[Interface]:
    """Fallback for ``ip -o addr show`` / ``ip -o link show`` text output."""
    by_name: Dict[str, Interface] = {}
    for line in link_text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[1].rstrip(":").split("@")[0]
        iface = by_name.setdefault(name, Interface(name=name))
        iface.up = "LOWER_UP" in line or "state UP" in line
        iface.admin_up = "<" in line and ",UP" in line or "<UP" in line
        if "LOOPBACK" in line:
            iface.kind = KIND_LOOPBACK
        m = re.search(r"link/\S+\s+([0-9a-f:]{17})", line)
        if m:
            iface.mac = normalize_mac(m.group(1))
    for line in addr_text.splitlines():
        m = _ADDR_TEXT_RE.match(line)
        if not m:
            continue
        name, fam, cidr = m.groups()
        iface = by_name.setdefault(name, Interface(name=name))
        if name == "lo":
            iface.kind = KIND_LOOPBACK
        ip, _, plen = cidr.partition("/")
        iface.addresses.append(
            Address(ip=ip, prefixlen=int(plen or 32), family=6 if fam == "inet6" else 4)
        )
        if not link_text:
            iface.up = True  # no link info: assume an addressed NIC is usable
    return list(by_name.values())


def parse_route_text(text: str) -> List[Route]:
    routes: List[Route] = []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        dst = parts[0]
        if dst == "default":
            dst = "0.0.0.0/0"
        elif "/" not in dst:
            dst = f"{dst}/32"
        r = Route(destination=dst)
        if "via" in parts:
            r.gateway = parts[parts.index("via") + 1]
        if "dev" in parts:
            r.interface = parts[parts.index("dev") + 1]
        if "metric" in parts:
            try:
                r.metric = int(parts[parts.index("metric") + 1])
            except (ValueError, IndexError):
                pass
        routes.append(r)
    return routes


def parse_neigh_text(text: str) -> List[Neighbor]:
    out: List[Neighbor] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2 or ":" in parts[0]:
            continue
        n = Neighbor(
            ip=parts[0], state=_NEIGH_STATES.get(parts[-1].upper(), NEIGH_UNKNOWN)
        )
        if "lladdr" in parts:
            n.mac = normalize_mac(parts[parts.index("lladdr") + 1])
        if "dev" in parts:
            n.interface = parts[parts.index("dev") + 1]
        out.append(n)
    return out


def parse_resolv_conf(text: str) -> List[str]:
    servers: List[str] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            servers.append(parts[1])
    return servers


# --------------------------------------------------------------------------- collect


def resolvers() -> List[str]:
    servers = parse_resolv_conf(_read(Path("/etc/resolv.conf")) or "")
    if servers and all(s.startswith("127.0.0.53") for s in servers):
        # systemd-resolved stub: report the real upstream servers it forwards to.
        upstream = parse_resolv_conf(
            _read(Path("/run/systemd/resolve/resolv.conf")) or ""
        )
        if upstream:
            return upstream
    return servers


def collect() -> HostNetwork:
    hn = HostNetwork(source="linux")
    if not have("ip"):
        hn.errors.append("'ip' command not found (install iproute2)")
        hn.resolvers = resolvers()
        return hn
    addr = _ip_json("addr", "show")
    if addr is not None:
        hn.interfaces = parse_addr_json(addr)
        hn.source = "linux:ip-json"
    else:
        a = run(["ip", "-o", "addr", "show"], timeout=8)
        link = run(["ip", "-o", "link", "show"], timeout=8)
        hn.interfaces = parse_addr_text(a.stdout, link.stdout if link.ok else "")
        for iface in hn.interfaces:
            _apply_sysfs(iface)
        hn.source = "linux:ip-text"
    route = _ip_json("route", "show")
    if route is not None:
        hn.routes = parse_route_json(route)
    else:
        hn.routes = parse_route_text(run(["ip", "route", "show"], timeout=8).stdout)
    hn.resolvers = resolvers()
    return hn


def neighbors() -> List[Neighbor]:
    if not have("ip"):
        return []
    data = _ip_json("-s", "neigh", "show")
    if data is not None:
        return parse_neigh_json(data)
    return parse_neigh_text(run(["ip", "neigh", "show"], timeout=8).stdout)


def neighbor(ip: str) -> Optional[Neighbor]:
    if not have("ip"):
        return None
    data = _ip_json("-s", "neigh", "show", "to", ip)
    found = (
        parse_neigh_json(data)
        if data is not None
        else parse_neigh_text(run(["ip", "neigh", "show", "to", ip], timeout=5).stdout)
    )
    for n in found:
        if n.ip == ip:
            return n
    return None


def link_stats() -> Dict[str, LinkStats]:
    out: Dict[str, LinkStats] = {}
    if not SYS_NET.is_dir():
        return out
    for d in SYS_NET.iterdir():
        st = read_link_stats(d.name)
        if st is not None:
            out[d.name] = st
    return out
