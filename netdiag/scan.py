"""Discover the devices on a subnet (ping sweep + ARP), safely.

"What is actually connected to this line?" is the first question when taking
over equipment or hunting an IP conflict. A sweep sends one or two ICMP echo
requests per address at a gentle, capped rate, then reads the ARP table: on the
local subnet every powered device has to answer ARP, so devices that ignore
ping are found too. No TCP port is touched unless ports are asked for
explicitly — port scanning can upset fragile controllers.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import netinfo, oui
from .inventory import Device
from .measure import PingStats, ping_many, tcp_check_many
from .netinfo import model as nm

DEFAULT_MAX_HOSTS = 512  # a /23; stays below Linux' default ARP table limits


@dataclass
class ScanHit:
    ip: str
    mac: Optional[str] = None
    vendor: Optional[str] = None
    replied: bool = False  # answered ICMP echo
    arp_only: bool = False  # silent to ping, but answered ARP
    rtt_ms: Optional[float] = None
    hostname: Optional[str] = None
    ports: Dict[int, str] = field(default_factory=dict)  # port -> state
    is_self: bool = False
    is_gateway: bool = False

    def to_dict(self) -> dict:
        return {
            "ip": self.ip,
            "mac": self.mac,
            "vendor": self.vendor,
            "replied": self.replied,
            "arp_only": self.arp_only,
            "rtt_ms": round(self.rtt_ms, 3) if self.rtt_ms is not None else None,
            "hostname": self.hostname,
            "ports": {str(k): v for k, v in self.ports.items()},
            "is_self": self.is_self,
            "is_gateway": self.is_gateway,
        }


@dataclass
class ScanResult:
    networks: List[str]
    hits: List[ScanHit]
    probed: int
    duplicates: List[Tuple[str, List[str]]] = field(default_factory=list)  # ip, macs
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "networks": self.networks,
            "probed": self.probed,
            "found": len(self.hits),
            "hits": [h.to_dict() for h in self.hits],
            "duplicates": [{"ip": ip, "macs": macs} for ip, macs in self.duplicates],
            "notes": self.notes,
        }

    def to_devices(self) -> List[Device]:
        return [
            Device(name="", host=h.ip, mac=h.mac, note=h.vendor or "")
            for h in self.hits
            if not h.is_self
        ]


def default_networks(
    hn: netinfo.HostNetwork, max_hosts: int = DEFAULT_MAX_HOSTS
) -> Tuple[List[ipaddress.IPv4Network], List[str]]:
    """The subnets of this PC's physical, connected NICs (bounded in size).

    A subnet larger than *max_hosts* is narrowed to the /24 around the PC's own
    address, with a note — sweeping a /16 by accident would take minutes and is
    rude on a production network.
    """
    nets: List[ipaddress.IPv4Network] = []
    notes: List[str] = []
    lan = [
        i for i in hn.lan_interfaces() if i.kind != netinfo.KIND_VIRTUAL
    ] or hn.lan_interfaces()
    for iface in lan:
        for a in iface.usable_ipv4:
            net = a.network
            if net.num_addresses - 2 > max_hosts:
                narrowed = ipaddress.ip_interface(f"{a.ip}/24").network
                notes.append(f"{iface.name}: {net} -> {narrowed}")
                net = narrowed
            if net.prefixlen < 31 and net not in nets:
                nets.append(net)
    return nets, notes


def parse_networks(specs: Sequence[str]) -> List[ipaddress.IPv4Network]:
    """``192.168.3.0/24``, ``192.168.3.10`` (one host) or ``192.168.3.10-50``."""
    nets: List[ipaddress.IPv4Network] = []
    for spec in specs:
        spec = spec.strip()
        if "-" in spec and "/" not in spec:
            start_s, end_s = spec.split("-", 1)
            start = ipaddress.ip_address(start_s)
            if "." not in end_s:
                end_s = ".".join(start_s.split(".")[:3] + [end_s])
            end = ipaddress.ip_address(end_s)
            nets.extend(ipaddress.summarize_address_range(start, end))
        else:
            nets.append(ipaddress.ip_network(spec, strict=False))
    return [n for n in nets if n.version == 4]


def _hosts(nets: Sequence[ipaddress.IPv4Network]) -> List[str]:
    out: List[str] = []
    for n in nets:
        hosts = [str(h) for h in n.hosts()] if n.prefixlen < 31 else [str(h) for h in n]
        out.extend(hosts)
    return list(dict.fromkeys(out))


def _reverse_name(ip: str, timeout: float = 1.0) -> Optional[str]:
    result: List[Optional[str]] = [None]

    def _lookup() -> None:
        try:
            result[0] = socket.gethostbyaddr(ip)[0]
        except (OSError, UnicodeError):
            result[0] = None

    t = threading.Thread(target=_lookup, daemon=True)
    t.start()
    t.join(timeout)
    return result[0]


def scan(
    nets: Sequence[ipaddress.IPv4Network],
    count: int = 2,
    timeout: float = 1.0,
    ports: Sequence[int] = (),
    resolve_names: bool = False,
    max_hosts: int = DEFAULT_MAX_HOSTS,
) -> ScanResult:
    hosts = _hosts(nets)
    if len(hosts) > max_hosts:
        raise ValueError(
            f"{len(hosts)} addresses exceed the limit of {max_hosts} (use --max-hosts)"
        )
    hn = netinfo.snapshot()
    own = set(hn.local_ips())
    gateways = set(hn.default_gateways())
    before = {n.ip: n for n in netinfo.neighbors()}
    t0 = time.monotonic()
    stats: Dict[str, PingStats] = (
        ping_many(hosts, count=count, interval=0.25, timeout=timeout) if hosts else {}
    )
    window = time.monotonic() - t0 + 1.0
    after_list = netinfo.neighbors()
    after: Dict[str, nm.Neighbor] = {}
    macs_by_ip: Dict[str, set] = {}
    for n in after_list:
        after[n.ip] = n
        if n.mac and not nm.mac_is_null(n.mac):
            macs_by_ip.setdefault(n.ip, set()).add(n.mac)

    hostset = set(hosts)
    hits: List[ScanHit] = []
    for ip in hosts:
        st = stats.get(ip)
        n = after.get(ip)
        replied = bool(st and st.any_reply)
        # ARP evidence must come from *this* sweep: an entry that appeared
        # during it, or one confirmed within the sweep window (Linux reports the
        # age). A leftover cache entry may belong to a device that is gone.
        fresh_arp = False
        if n is not None and n.mac and not nm.mac_is_null(n.mac):
            recently = n.state == nm.NEIGH_REACHABLE and (
                n.confirmed_age is not None and n.confirmed_age <= window
            )
            fresh_arp = recently or ip not in before
        if not (replied or fresh_arp or ip in own):
            continue
        hit = ScanHit(
            ip=ip,
            mac=n.mac if n is not None else None,
            replied=replied,
            arp_only=not replied and fresh_arp,
            rtt_ms=(st.rtt.median if st is not None and st.rtt is not None else None),
            is_self=ip in own,
            is_gateway=ip in gateways,
        )
        if ip in own:
            iface = next(
                (i for i in hn.interfaces if any(a.ip == ip for a in i.ipv4)), None
            )
            hit.mac = hit.mac or (iface.mac if iface else None)
        hit.vendor = oui.lookup(hit.mac)
        hits.append(hit)

    if resolve_names and hits:
        with ThreadPoolExecutor(max_workers=16) as pool:
            for hit, name in zip(hits, pool.map(lambda h: _reverse_name(h.ip), hits)):
                hit.hostname = name
    if ports and hits:
        pairs = [(h.ip, p) for h in hits if not h.is_self for p in ports]
        res = tcp_check_many(pairs, attempts=1, timeout=1.0, max_workers=16)
        for h in hits:
            for p in ports:
                pc = res.get((h.ip, p))
                if pc is not None:
                    h.ports[p] = pc.state
    dups = sorted(
        (ip, sorted(m)) for ip, m in macs_by_ip.items() if len(m) > 1 and ip in hostset
    )
    hits.sort(key=lambda h: ipaddress.ip_address(h.ip))
    return ScanResult(
        networks=[str(n) for n in nets], hits=hits, probed=len(hosts), duplicates=dups
    )
