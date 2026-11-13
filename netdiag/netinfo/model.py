"""OS-neutral model of this host's network configuration.

Every platform collector (Linux ``ip -j``/sysfs, Windows PowerShell, macOS
``ifconfig``/``netstat``) fills the same dataclasses, so probes reason about
interfaces, routes and ARP entries without branching on the OS.
"""

from __future__ import annotations

import ipaddress
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Normalised neighbour (ARP) states. Only REACHABLE/PERMANENT prove the device
# answered ARP recently; STALE/DELAY/PROBE are inconclusive (old cache entries);
# INCOMPLETE/FAILED mean ARP got no answer.
NEIGH_REACHABLE = "reachable"
NEIGH_PERMANENT = "permanent"
NEIGH_STALE = "stale"
NEIGH_DELAY = "delay"
NEIGH_PROBE = "probe"
NEIGH_INCOMPLETE = "incomplete"
NEIGH_FAILED = "failed"
NEIGH_UNKNOWN = "unknown"

NEIGH_CONFIRMED = {NEIGH_REACHABLE, NEIGH_PERMANENT}
NEIGH_ABSENT = {NEIGH_INCOMPLETE, NEIGH_FAILED}

KIND_ETHERNET = "ethernet"
KIND_WIFI = "wifi"
KIND_LOOPBACK = "loopback"
KIND_VIRTUAL = "virtual"
KIND_OTHER = "other"

# Interface flags (Linux ``ip``, macOS ``ifconfig``) of links without ARP.
NO_ARP_FLAGS = frozenset({"NOARP", "POINTOPOINT"})


def normalize_mac(mac: Optional[str]) -> Optional[str]:
    """``AA-BB-CC-D-E-F`` / ``aa:bb:cc:dd:ee:ff`` -> ``aa:bb:cc:0d:0e:0f``.

    macOS drops leading zeros and Windows uses dashes; normalising lets MACs
    from different tools (and runs) be compared directly.
    """
    if not mac:
        return None
    m = mac.strip().lower().replace("-", ":")
    if m in ("", "(incomplete)", "incomplete", "<incomplete>"):
        return None
    parts = m.split(":")
    if len(parts) == 6 and all(1 <= len(p) <= 2 for p in parts):
        try:
            return ":".join(f"{int(p, 16):02x}" for p in parts)
        except ValueError:
            return None
    if len(m) == 12 and all(c in "0123456789abcdef" for c in m):
        return ":".join(m[i : i + 2] for i in range(0, 12, 2))
    return None


def mac_is_null(mac: Optional[str]) -> bool:
    return not mac or mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff")


def mac_is_local(mac: Optional[str]) -> bool:
    """Locally administered (random / virtual) MAC: bit 1 of the first octet."""
    if not mac:
        return False
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except ValueError:
        return False


@dataclass
class Address:
    ip: str
    prefixlen: int
    family: int = 4
    flags: List[str] = field(default_factory=list)  # dhcp, manual, duplicate, ...

    @property
    def interface(self) -> ipaddress.IPv4Interface:
        return ipaddress.ip_interface(f"{self.ip}/{self.prefixlen}")

    @property
    def network(self):
        return self.interface.network

    @property
    def is_link_local(self) -> bool:
        return self.interface.ip.is_link_local

    @property
    def is_loopback(self) -> bool:
        return self.interface.ip.is_loopback

    @property
    def cidr(self) -> str:
        return f"{self.ip}/{self.prefixlen}"


@dataclass
class LinkStats:
    rx_packets: Optional[int] = None
    tx_packets: Optional[int] = None
    rx_errors: Optional[int] = None
    tx_errors: Optional[int] = None
    rx_dropped: Optional[int] = None
    tx_dropped: Optional[int] = None
    rx_crc_errors: Optional[int] = None
    collisions: Optional[int] = None
    # Times the link went down/up (Linux sysfs carrier_changes counts both
    # edges, so one drop-and-return adds 2). A change during a measurement means
    # this PC's own cable/port/NIC flapped.
    carrier_changes: Optional[int] = None

    # rx_crc_errors is a subset of rx_errors (Linux counts it in both), so it
    # stays out of the total.
    ERROR_FIELDS = ("rx_errors", "tx_errors", "collisions")

    def link_drops(self) -> int:
        """Down-and-up events implied by ``carrier_changes`` (rounded up)."""
        return ((self.carrier_changes or 0) + 1) // 2

    def errors_total(self) -> int:
        return sum(getattr(self, f) or 0 for f in self.ERROR_FIELDS)

    def delta(self, before: "LinkStats") -> "LinkStats":
        """Per-counter increase since *before* (None where either side is unknown)."""
        out = LinkStats()
        for name in asdict(self):
            a, b = getattr(before, name), getattr(self, name)
            setattr(out, name, (b - a) if a is not None and b is not None else None)
        return out

    def to_dict(self) -> Dict[str, Optional[int]]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Interface:
    name: str
    index: Optional[int] = None
    description: str = ""
    mac: Optional[str] = None
    up: bool = False  # operational: link/carrier present
    admin_up: Optional[bool] = None
    kind: str = KIND_OTHER
    speed_mbps: Optional[float] = None
    duplex: Optional[str] = None  # "full" | "half"
    mtu: Optional[int] = None
    addresses: List[Address] = field(default_factory=list)
    stats: Optional[LinkStats] = None
    # False on links that resolve no neighbours (WireGuard, PPP, tun, other
    # tunnels): a missing ARP entry there says nothing about the peer.
    arp: bool = True

    @property
    def ipv4(self) -> List[Address]:
        return [a for a in self.addresses if a.family == 4]

    @property
    def usable_ipv4(self) -> List[Address]:
        """IPv4 addresses that can talk to a LAN (not loopback/169.254/duplicate)."""
        return [
            a
            for a in self.ipv4
            if not a.is_loopback and not a.is_link_local and "duplicate" not in a.flags
        ]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["addresses"] = [a.cidr for a in self.addresses]
        d["address_flags"] = {a.cidr: a.flags for a in self.addresses if a.flags}
        d["stats"] = self.stats.to_dict() if self.stats else None
        return d


@dataclass
class Route:
    destination: str  # CIDR; "0.0.0.0/0" for the default route
    gateway: Optional[str] = None
    interface: Optional[str] = None
    metric: Optional[int] = None

    @property
    def is_default(self) -> bool:
        return self.destination in ("0.0.0.0/0", "default")

    @property
    def network(self) -> Optional[ipaddress.IPv4Network]:
        """The destination as an IPv4 network (None if it is not one)."""
        try:
            net = ipaddress.ip_network(
                "0.0.0.0/0" if self.is_default else self.destination, strict=False
            )
        except ValueError:
            return None
        return net if net.version == 4 else None

    @property
    def rank(self) -> int:
        """Metric order between routes of equal prefix; an unknown metric loses."""
        return self.metric if self.metric is not None else 1 << 30

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Neighbor:
    ip: str
    mac: Optional[str] = None
    state: str = NEIGH_UNKNOWN
    interface: Optional[str] = None
    confirmed_age: Optional[float] = None  # seconds since last ARP confirmation (Linux)

    @property
    def confirmed(self) -> bool:
        return self.state in NEIGH_CONFIRMED and not mac_is_null(self.mac)

    @property
    def absent(self) -> bool:
        return self.state in NEIGH_ABSENT or (
            self.mac is None and self.state != NEIGH_UNKNOWN
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RouteDecision:
    """How this host would reach one target address."""

    target: str
    on_link: bool = False
    interface: Optional[Interface] = None
    address: Optional[Address] = None  # local address in the target's subnet
    gateway: Optional[str] = None  # set when the target is reached via a router
    candidates: List[Tuple[Interface, Address]] = field(default_factory=list)
    special: Optional[str] = None  # self | network | broadcast | loopback | ...

    @property
    def arp(self) -> bool:
        """ARP can confirm or rule out the target: it sits on an ARP link of this PC.

        False for targets behind a gateway, behind a tunnel or point-to-point
        link, or on a link the collector could not identify.
        """
        return (
            self.on_link
            and self.special is None
            and self.interface is not None
            and self.interface.arp
        )


@dataclass
class HostNetwork:
    """A snapshot of interfaces, routes and resolvers (collected once per run)."""

    interfaces: List[Interface] = field(default_factory=list)
    routes: List[Route] = field(default_factory=list)
    resolvers: List[str] = field(default_factory=list)
    source: str = ""
    errors: List[str] = field(default_factory=list)
    # Platform-specific facts gathered in the same collection pass (e.g. the
    # Windows firewall profiles), so probes need no extra PowerShell start.
    extras: Dict[str, Any] = field(default_factory=dict)

    # -- queries ---------------------------------------------------------------
    def interface(self, name: Optional[str]) -> Optional[Interface]:
        for i in self.interfaces:
            if i.name == name:
                return i
        return None

    def default_routes(self) -> List[Route]:
        return [r for r in self.routes if r.is_default and r.gateway]

    def preferred_default_route(self) -> Optional[Route]:
        """The default route the OS normally uses: lowest metric, an unset one last."""
        return min(self.default_routes(), key=lambda r: r.rank, default=None)

    def default_gateways(self) -> List[str]:
        return list(dict.fromkeys(r.gateway for r in self.default_routes()))

    def lan_interfaces(self) -> List[Interface]:
        """Non-loopback interfaces that are up and hold a usable IPv4 address."""
        return [
            i
            for i in self.interfaces
            if i.kind != KIND_LOOPBACK and i.up and i.usable_ipv4
        ]

    def local_ips(self) -> List[str]:
        return [a.ip for i in self.interfaces for a in i.ipv4]

    def decide_route(self, target_ip: str) -> RouteDecision:
        """Classify how *target_ip* is reached: on-link (which NIC) or via a gateway."""
        dec = RouteDecision(target=target_ip)
        try:
            ip = ipaddress.ip_address(target_ip)
        except ValueError:
            dec.special = "invalid"
            return dec
        if ip.version != 4:
            dec.special = "ipv6"
            return dec
        if ip.is_loopback:
            dec.special = "loopback"
            return dec
        if ip.is_multicast or ip == ipaddress.IPv4Address("255.255.255.255"):
            dec.special = "broadcast"
            return dec

        for iface in self.interfaces:
            if iface.kind == KIND_LOOPBACK:
                continue
            for addr in iface.ipv4:
                if addr.is_loopback:
                    continue
                if addr.ip == target_ip:
                    dec.special = "self"
                    dec.interface, dec.address = iface, addr
                    return dec
                net = addr.network
                if ip in net:
                    dec.candidates.append((iface, addr))
        # Prefer candidates whose link is up, then the most specific prefix.
        dec.candidates.sort(key=lambda c: (not c[0].up, -c[1].prefixlen))
        if dec.candidates:
            iface, addr = dec.candidates[0]
            net = addr.network
            if net.prefixlen <= 30 and ip == net.network_address:
                dec.special = "network"
            elif net.prefixlen <= 30 and ip == net.broadcast_address:
                dec.special = "broadcast"
            dec.on_link = True
            dec.interface, dec.address = iface, addr
            return dec

        best = self._route_to(ip)
        if best is not None:
            dec.gateway = best.gateway
            dec.interface = self.interface(best.interface)
            if dec.interface and dec.interface.usable_ipv4:
                dec.address = dec.interface.usable_ipv4[0]
            if not best.gateway:  # an explicit on-link route without an address
                dec.on_link = True
        return dec

    def _route_to(self, ip: ipaddress.IPv4Address) -> Optional[Route]:
        """The route the OS picks off the local subnets: most specific, then lowest metric."""
        matches = [
            (r, net)
            for r in self.routes
            if (net := r.network) is not None and ip in net
        ]
        best = min(
            matches, key=lambda m: (-m[1].prefixlen, m[0].rank), default=(None, None)
        )
        return best[0]

    def to_dict(self) -> dict:
        return {
            "interfaces": [i.to_dict() for i in self.interfaces],
            "routes": [r.to_dict() for r in self.routes],
            "resolvers": list(self.resolvers),
            "source": self.source,
            "errors": list(self.errors),
        }
