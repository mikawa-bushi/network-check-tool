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
from typing import Dict, Iterator, List, Optional, Sequence, Tuple, Union

from . import netinfo, oui
from .core import InputError
from .i18n import tr
from .inventory import Device, InventoryError, normalize_host
from .measure import PingStats, ping_many, tcp_check_many
from .netinfo import model as nm

DEFAULT_MAX_HOSTS = 512  # a /23; stays below Linux' default ARP table limits


class ScanError(InputError, ValueError):
    """A scan range netdiag will not sweep (message says why)."""


@dataclass(frozen=True)
class AddressRange:
    """``192.168.3.10-50``: every address from *start* to *end*, inclusive."""

    start: ipaddress.IPv4Address
    end: ipaddress.IPv4Address

    @property
    def num_addresses(self) -> int:
        return int(self.end) - int(self.start) + 1

    def addresses(self) -> Iterator[ipaddress.IPv4Address]:
        for n in range(int(self.start), int(self.end) + 1):
            yield ipaddress.IPv4Address(n)

    def __str__(self) -> str:
        return f"{self.start}-{self.end}"


ScanTarget = Union[ipaddress.IPv4Network, AddressRange]


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
            Device(
                name="",
                host=h.ip,
                ports=sorted(p for p, state in h.ports.items() if state == "open"),
                mac=h.mac,
                note=h.vendor or "",
            )
            for h in self.hits
            if not h.is_self
        ]


def default_networks(
    hn: netinfo.HostNetwork, max_hosts: int = DEFAULT_MAX_HOSTS
) -> Tuple[List[ScanTarget], List[str]]:
    """The subnets of this PC's physical, connected NICs (each bounded in size).

    A subnet larger than *max_hosts* is narrowed to the /24 around the PC's own
    address, with a note — sweeping a /16 by accident would take minutes and is
    rude on a production network. The bound is per NIC: a PC on an office
    network and two lines sweeps all three.
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


def _ipv4(text: str, spec: str) -> ipaddress.IPv4Address:
    """One address of a scan range, zero-padded parts read as decimal."""
    try:
        addr = ipaddress.ip_address(normalize_host(text))
    except (InventoryError, ValueError) as e:
        raise ScanError(
            tr(
                f"走査範囲を読めません: {spec}（例: 192.168.3.0/24、192.168.3.10-50）",
                f"cannot read the scan range {spec} (e.g. 192.168.3.0/24 or 192.168.3.10-50)",
            )
        ) from e
    if addr.version != 4:
        raise ScanError(
            tr(
                f"IPv6 の範囲は走査できません: {spec}（scan は IPv4 のサブネットを走査します）",
                f"IPv6 ranges are not supported: {spec} (scan sweeps IPv4 subnets)",
            )
        )
    return addr


def parse_networks(specs: Sequence[str]) -> List[ScanTarget]:
    """``192.168.3.0/24``, ``192.168.3.10`` (one host) or ``192.168.3.10-50``.

    Raises :class:`ScanError` for anything else, IPv6 included.
    """
    targets: List[ScanTarget] = []
    for spec in specs:
        spec = spec.strip()
        if "-" in spec and "/" not in spec:
            start_s, end_s = (s.strip() for s in spec.split("-", 1))
            start = _ipv4(start_s, spec)
            if "." not in end_s:
                end_s = ".".join(str(start).split(".")[:3] + [end_s])
            end = _ipv4(end_s, spec)
            if end < start:
                raise ScanError(
                    tr(
                        f"走査範囲の終わりが始まりより前です: {spec}",
                        f"the scan range ends before it starts: {spec}",
                    )
                )
            targets.append(AddressRange(start, end))
        else:
            addr, _, prefix = spec.partition("/")
            base = _ipv4(addr, spec)
            try:
                targets.append(
                    ipaddress.ip_network(f"{base}/{prefix or 32}", strict=False)
                )
            except ValueError as e:
                raise ScanError(
                    tr(
                        f"走査範囲を読めません: {spec}",
                        f"cannot read the scan range {spec}",
                    )
                ) from e
    return targets


def _hosts(nets: Sequence[ScanTarget]) -> List[str]:
    out: List[str] = []
    for n in nets:
        if isinstance(n, AddressRange):
            out.extend(str(a) for a in n.addresses())
        elif n.prefixlen < 31:
            out.extend(str(h) for h in n.hosts())
        else:
            out.extend(str(h) for h in n)
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
    nets: Sequence[ScanTarget],
    count: int = 2,
    timeout: float = 1.0,
    ports: Sequence[int] = (),
    resolve_names: bool = False,
    max_hosts: Optional[int] = DEFAULT_MAX_HOSTS,
) -> ScanResult:
    """Sweep *nets*. ``max_hosts`` None means each range is already bounded
    (the default networks of :func:`default_networks`)."""
    hosts = _hosts(nets)
    if max_hosts is not None and len(hosts) > max_hosts:
        raise ScanError(
            tr(
                f"{len(hosts)} アドレスは上限 {max_hosts} を超えています（--max-hosts で変更）",
                f"{len(hosts)} addresses exceed the limit of {max_hosts} (use --max-hosts)",
            )
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
    after: Dict[str, nm.Neighbor] = {n.ip: n for n in after_list}

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
    # An IP conflict shows as two answers to one ping. Different MACs for one
    # address on two NICs are two separate networks, not a conflict.
    dups = sorted(
        (
            ip,
            sorted(
                {n.mac for n in after_list if n.ip == ip and not nm.mac_is_null(n.mac)}
            ),
        )
        for ip, st in stats.items()
        if st.duplicates
    )
    hits.sort(key=lambda h: ipaddress.ip_address(h.ip))
    return ScanResult(
        networks=[str(n) for n in nets], hits=hits, probed=len(hosts), duplicates=dups
    )
