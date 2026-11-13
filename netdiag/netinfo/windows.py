"""Windows collector: one PowerShell call returning JSON, with text fallbacks.

PowerShell's NetTCPIP/NetAdapter cmdlets expose everything needed (link speed,
duplex, IPv4 address *state* — including ``Duplicate`` when Windows detected an
IP conflict — routes, ARP cache, DNS servers, error counters) and ``ConvertTo-
Json`` makes the result independent of the display language. Enum values are
stringified inside the script so they arrive as names, not integers.

When PowerShell is unavailable or blocked, ``route print`` and ``arp -a`` still
give addresses, masks, gateways and ARP entries; their parsers only rely on
IP/MAC/number tokens, so Japanese and English output parse the same way.
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any, Dict, List, Optional

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

_PS_PREAMBLE = (
    "$ErrorActionPreference='SilentlyContinue';"
    "$ProgressPreference='SilentlyContinue';"
    "[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false);"
)

# Every frame counts towards the error ratio, as Linux rx_packets does: an
# EtherNet/IP or PROFINET line is mostly multicast.
_STATS_SELECT = (
    "@(Get-NetAdapterStatistics | Select-Object Name,ReceivedUnicastPackets,"
    "ReceivedMulticastPackets,ReceivedBroadcastPackets,SentUnicastPackets,"
    "SentMulticastPackets,SentBroadcastPackets,ReceivedPacketErrors,"
    "OutboundPacketErrors,ReceivedDiscardedPackets,OutboundDiscardedPackets)"
)

COLLECT_SCRIPT = _PS_PREAMBLE + (
    "$a=@(Get-NetAdapter -IncludeHidden:$false | Select-Object Name,"
    "InterfaceDescription,ifIndex,@{n='Status';e={\"$($_.Status)\"}},MacAddress,"
    "LinkSpeed,ReceiveLinkSpeed,TransmitLinkSpeed,FullDuplex,MtuSize,Virtual,"
    "HardwareInterface,@{n='MediaType';e={\"$($_.MediaType)\"}},"
    "@{n='PhysicalMediaType';e={\"$($_.PhysicalMediaType)\"}});"
    "$i=@(Get-NetIPAddress -AddressFamily IPv4 | Select-Object IPAddress,"
    "PrefixLength,InterfaceIndex,InterfaceAlias,"
    "@{n='AddressState';e={\"$($_.AddressState)\"}},"
    "@{n='PrefixOrigin';e={\"$($_.PrefixOrigin)\"}});"
    "$r=@(Get-NetRoute -AddressFamily IPv4 -PolicyStore ActiveStore | "
    "Select-Object DestinationPrefix,"
    "NextHop,InterfaceIndex,InterfaceAlias,RouteMetric,InterfaceMetric);"
    "$d=@(Get-DnsClientServerAddress -AddressFamily IPv4 | Select-Object "
    "InterfaceIndex,ServerAddresses);"
    f"$s={_STATS_SELECT};"
    "$f=@(Get-NetFirewallProfile | Select-Object Name,"
    "@{n='Enabled';e={\"$($_.Enabled)\"}},"
    "@{n='DefaultInboundAction';e={\"$($_.DefaultInboundAction)\"}},"
    "@{n='DefaultOutboundAction';e={\"$($_.DefaultOutboundAction)\"}});"
    "[pscustomobject]@{adapters=$a;addresses=$i;routes=$r;dns=$d;stats=$s;firewall=$f}"
    " | ConvertTo-Json -Depth 4 -Compress"
)

NEIGH_SCRIPT = _PS_PREAMBLE + (
    "@(Get-NetNeighbor -AddressFamily IPv4 | Select-Object IPAddress,"
    "LinkLayerAddress,InterfaceIndex,InterfaceAlias,"
    "@{n='State';e={\"$($_.State)\"}}) | ConvertTo-Json -Depth 3 -Compress"
)

_NEIGH_STATES = {
    "reachable": NEIGH_REACHABLE,
    "permanent": NEIGH_PERMANENT,
    "stale": NEIGH_STALE,
    "delay": NEIGH_DELAY,
    "probe": NEIGH_PROBE,
    "incomplete": NEIGH_INCOMPLETE,
    "unreachable": NEIGH_FAILED,
}

# ``Get-NetNeighbor`` State as integers, if a host ever serialises the raw enum.
_NEIGH_STATE_INTS = {
    0: NEIGH_FAILED,  # Unreachable
    1: NEIGH_INCOMPLETE,
    2: NEIGH_PROBE,
    3: NEIGH_DELAY,
    4: NEIGH_STALE,
    5: NEIGH_REACHABLE,
    6: NEIGH_PERMANENT,
}


# NDIS media that carry IP without Ethernet framing, so no ARP: WireGuard and
# Tailscale (wintun), mobile broadband, dial-up/VPN miniports.
_NO_ARP_MEDIA = {"ip", "tunnel", "wan", "wireless wan", "cowan"}


def _powershell() -> Optional[str]:
    for exe in ("powershell", "pwsh"):
        if have(exe):
            return exe
    return None


def powershell_args(script: str) -> List[str]:
    """Argument vector that runs *script* in Windows PowerShell (else pwsh)."""
    return [
        _powershell() or "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]


def run_powershell(script: str, timeout: float = 30.0) -> Optional[Any]:
    """Run *script* and parse its JSON stdout; None on any failure."""
    if not _powershell():
        return None
    res = run(powershell_args(script), timeout=timeout)
    text = (res.stdout or "").strip().lstrip("﻿")
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _as_list(value: Any) -> List[Any]:
    """ConvertTo-Json emits a bare object for one item and null for none."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_link_speed(value: Any) -> Optional[float]:
    """``1000000000`` (bit/s) or ``"1 Gbps"``/``"100 Mbps"`` -> Mbit/s."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) / 1e6 if value > 0 else None
    m = re.match(r"\s*([\d.]+)\s*([KMGT]?)bps", str(value), re.IGNORECASE)
    if not m:
        return None
    scale = {"": 1e-6, "K": 1e-3, "M": 1.0, "G": 1e3, "T": 1e6}[m.group(2).upper()]
    return float(m.group(1)) * scale


def _kind(adapter: Dict[str, Any]) -> str:
    desc = (
        f"{adapter.get('InterfaceDescription', '')} {adapter.get('Name', '')}".lower()
    )
    media = (
        f"{adapter.get('PhysicalMediaType', '')} {adapter.get('MediaType', '')}".lower()
    )
    if "loopback" in desc:
        return KIND_LOOPBACK
    if (
        "802.11" in media
        or "wireless" in media
        or "wi-fi" in desc
        or "wireless" in desc
    ):
        return KIND_WIFI
    if adapter.get("Virtual") is True or adapter.get("HardwareInterface") is False:
        return KIND_VIRTUAL
    if any(
        k in desc
        for k in (
            "hyper-v",
            "virtual",
            "vmware",
            "virtualbox",
            "tap-",
            "vpn",
            "wan miniport",
            "bluetooth",
        )
    ):
        return KIND_VIRTUAL
    if (
        "802.3" in media
        or "ethernet" in desc
        or adapter.get("HardwareInterface") is True
    ):
        return KIND_ETHERNET
    return KIND_OTHER


def _frames(row: Dict[str, Any], direction: str) -> Optional[int]:
    """Unicast + multicast + broadcast frames in one direction."""
    counts = [
        _int(row.get(f"{direction}{cast}Packets"))
        for cast in ("Unicast", "Multicast", "Broadcast")
    ]
    known = [c for c in counts if c is not None]
    return sum(known) if known else None


def parse_stats_json(items: Any) -> Dict[str, LinkStats]:
    """``Get-NetAdapterStatistics`` rows keyed by adapter name."""
    out: Dict[str, LinkStats] = {}
    for s in _as_list(items):
        out[str(s.get("Name"))] = LinkStats(
            rx_packets=_frames(s, "Received"),
            tx_packets=_frames(s, "Sent"),
            rx_errors=_int(s.get("ReceivedPacketErrors")),
            tx_errors=_int(s.get("OutboundPacketErrors")),
            rx_dropped=_int(s.get("ReceivedDiscardedPackets")),
            tx_dropped=_int(s.get("OutboundDiscardedPackets")),
        )
    return out


def parse_collect_json(data: Dict[str, Any]) -> HostNetwork:
    hn = HostNetwork(source="windows:powershell")
    by_index: Dict[int, Interface] = {}
    stats_by_name = parse_stats_json(data.get("stats"))
    for a in _as_list(data.get("adapters")):
        idx = _int(a.get("ifIndex"))
        status = str(a.get("Status") or "")
        iface = Interface(
            name=str(a.get("Name") or f"if{idx}"),
            index=idx,
            description=str(a.get("InterfaceDescription") or ""),
            mac=normalize_mac(a.get("MacAddress")),
            up=status.lower() == "up",
            admin_up=status.lower() != "disabled",
            kind=_kind(a),
            speed_mbps=parse_link_speed(a.get("ReceiveLinkSpeed"))
            or parse_link_speed(a.get("LinkSpeed")),
            mtu=_int(a.get("MtuSize")),
        )
        iface.arp = (
            iface.mac is not None
            and str(a.get("MediaType") or "").lower() not in _NO_ARP_MEDIA
        )
        if iface.up and isinstance(a.get("FullDuplex"), bool):
            iface.duplex = "full" if a["FullDuplex"] else "half"
        iface.stats = stats_by_name.get(iface.name)
        if idx is not None:
            by_index[idx] = iface
        hn.interfaces.append(iface)

    for ip in _as_list(data.get("addresses")):
        idx = _int(ip.get("InterfaceIndex"))
        iface = by_index.get(idx) if idx is not None else None
        if iface is None:
            # Not a network adapter: loopback, or a dial-up/VPN connection,
            # which carries IP without ARP.
            alias = str(ip.get("InterfaceAlias") or f"if{idx}")
            iface = Interface(name=alias, index=idx, up=True, arp=False)
            if alias.lower().startswith("loopback"):
                iface.kind = KIND_LOOPBACK
            if idx is not None:
                by_index[idx] = iface
            hn.interfaces.append(iface)
        flags: List[str] = []
        # Names when stringified in the script; enum integers otherwise
        # (AddressState 1=Tentative 2=Duplicate; PrefixOrigin 1=Manual 3=Dhcp).
        state = str(ip.get("AddressState") or "").lower()
        state = {"1": "tentative", "2": "duplicate"}.get(state, state)
        origin = str(ip.get("PrefixOrigin") or "").lower()
        origin = {"1": "manual", "3": "dhcp"}.get(origin, origin)
        if state == "duplicate":
            flags.append("duplicate")  # Windows' own IPv4 conflict detection
        elif state == "tentative" and iface.up:
            # A disconnected adapter keeps its address Tentative until it gets
            # a link; that is its normal resting state, not a pending check.
            flags.append("tentative")
        if origin == "dhcp":
            flags.append("dhcp")
        elif origin == "manual":
            flags.append("manual")
        try:
            iface.addresses.append(
                Address(
                    ip=str(ip.get("IPAddress")),
                    prefixlen=int(ip.get("PrefixLength")),
                    family=4,
                    flags=flags,
                )
            )
        except (TypeError, ValueError):
            continue

    names = {i.index: i.name for i in hn.interfaces}
    # Windows sends nothing through an adapter without a link, so its routes
    # and DNS servers are not in use.
    down = {i.index for i in hn.interfaces if not i.up and i.index is not None}
    if not _as_list(data.get("routes")):  # even an offline PC has loopback routes
        hn.errors.append("could not read the routing table (Get-NetRoute failed)")
    for r in _as_list(data.get("routes")):
        dst = str(r.get("DestinationPrefix") or "")
        idx = _int(r.get("InterfaceIndex"))
        if not dst or ":" in dst or idx in down:
            continue
        hop = str(r.get("NextHop") or "")
        metric = _int(r.get("RouteMetric"))
        ifm = _int(r.get("InterfaceMetric"))
        if metric is not None and ifm is not None:
            metric += ifm  # Windows picks routes by route + interface metric
        hn.routes.append(
            Route(
                destination=dst,
                gateway=None if hop in ("", "0.0.0.0") else hop,
                interface=names.get(idx) or r.get("InterfaceAlias"),
                metric=metric,
            )
        )

    servers: List[str] = []
    for d in _as_list(data.get("dns")):
        if _int(d.get("InterfaceIndex")) in down:
            continue
        for s in _as_list(d.get("ServerAddresses")):
            if s and s not in servers:
                servers.append(str(s))
    hn.resolvers = servers
    if data.get("firewall") is not None:
        hn.extras["firewall"] = _as_list(data.get("firewall"))
    return hn


def parse_neighbors_json(data: Any) -> List[Neighbor]:
    out: List[Neighbor] = []
    for n in _as_list(data):
        ip = n.get("IPAddress")
        if not ip:
            continue
        raw_state = n.get("State")
        if isinstance(raw_state, int):
            state = _NEIGH_STATE_INTS.get(raw_state, NEIGH_UNKNOWN)
        else:
            state = _NEIGH_STATES.get(str(raw_state).lower(), NEIGH_UNKNOWN)
        mac = normalize_mac(n.get("LinkLayerAddress"))
        if mac in ("00:00:00:00:00:00",):
            mac = None
        out.append(
            Neighbor(
                ip=str(ip), mac=mac, state=state, interface=n.get("InterfaceAlias")
            )
        )
    return out


# ------------------------------------------------------------------ text fallbacks

_IPV4 = r"\d{1,3}(?:\.\d{1,3}){3}"
_ARP_IFACE_RE = re.compile(rf"^\S.*?:\s*({_IPV4})\s+---\s+(0x[0-9a-fA-F]+)")
_ARP_ENTRY_RE = re.compile(
    rf"^\s+({_IPV4})\s+([0-9a-fA-F]{{2}}(?:-[0-9a-fA-F]{{2}}){{5}})\s+(\S+)"
)


def parse_arp_a(text: str) -> List[Neighbor]:
    """``arp -a`` in any language: ``  192.168.1.1   00-11-22-33-44-55   動的``.

    Only IP and MAC tokens are used; the localised type column is ignored (a
    listed entry has a resolved MAC, which is all the ARP evidence needed).
    """
    out: List[Neighbor] = []
    current_if: Optional[str] = None
    for line in text.splitlines():
        m = _ARP_IFACE_RE.match(line)
        if m:
            current_if = m.group(1)
            continue
        m = _ARP_ENTRY_RE.match(line)
        if not m:
            continue
        ip, mac = m.group(1), normalize_mac(m.group(2))
        if mac in ("ff:ff:ff:ff:ff:ff",) or ip.startswith(("224.", "239.", "255.")):
            continue
        out.append(Neighbor(ip=ip, mac=mac, state=NEIGH_STALE, interface=current_if))
    return out


_ROUTE_ROW_RE = re.compile(
    rf"^\s*({_IPV4})\s+({_IPV4})\s+(\S+)\s+({_IPV4})\s+(\d+)\s*$"
)


def parse_route_print(text: str) -> HostNetwork:
    """Build interfaces/routes from ``route print -4`` (language-independent).

    Rows are ``destination netmask gateway interface metric``; the gateway of an
    on-link route is a localised word ("On-link" / "リンク上"), i.e. any non-IP
    token. Local addresses and their masks are recovered from on-link subnet
    rows, which is enough for subnet/route decisions without PowerShell.
    """
    hn = HostNetwork(source="windows:route-print")
    by_ip: Dict[str, Interface] = {}
    for line in text.splitlines():
        m = _ROUTE_ROW_RE.match(line)
        if not m:
            continue
        dst, mask, gw, iface_ip, metric = m.groups()
        try:
            net = ipaddress.ip_network(f"{dst}/{mask}", strict=False)
        except ValueError:
            continue
        is_ip_gw = re.fullmatch(_IPV4, gw) is not None
        hn.routes.append(
            Route(
                destination=str(net),
                gateway=gw if is_ip_gw else None,
                interface=iface_ip,
                metric=int(metric),
            )
        )
        if iface_ip.startswith("127.") or net.is_multicast:
            continue
        iface = by_ip.setdefault(
            iface_ip, Interface(name=iface_ip, up=True, kind=KIND_OTHER)
        )
        if (
            not is_ip_gw
            and 0 < net.prefixlen < 32
            and ipaddress.ip_address(iface_ip) in net
        ):
            if not any(a.ip == iface_ip for a in iface.addresses):
                iface.addresses.append(Address(ip=iface_ip, prefixlen=net.prefixlen))
    for ip, iface in by_ip.items():
        if not iface.addresses:
            # Only a host route: a point-to-point link (PPP, WireGuard), no
            # subnet to ARP on.
            iface.addresses.append(Address(ip=ip, prefixlen=32))
            iface.arp = False
        hn.interfaces.append(iface)
    return hn


# --------------------------------------------------------------------------- collect


def collect() -> HostNetwork:
    data = run_powershell(COLLECT_SCRIPT)
    if isinstance(data, dict) and (data.get("adapters") or data.get("addresses")):
        return parse_collect_json(data)
    hn = parse_route_print(run(["route", "print", "-4"], timeout=10).stdout)
    hn.errors.append(
        "PowerShell network cmdlets unavailable; using 'route print' (no link "
        "speed/duplex/error counters)"
    )
    if not hn.routes:
        hn.errors.append("could not read the routing table ('route print' failed)")
    return hn


def neighbors() -> List[Neighbor]:
    """ARP cache via ``arp -a`` (fast: no PowerShell start-up per call).

    ``arp -a`` lists resolved entries only; an on-link address missing after a
    ping therefore means ARP got no answer, which is the evidence that matters.
    ``Get-NetNeighbor`` is the fallback when ``arp`` is unavailable.
    """
    res = run(["arp", "-a"], timeout=10)
    if res.found:
        return parse_arp_a(res.stdout)
    data = run_powershell(NEIGH_SCRIPT)
    return parse_neighbors_json(data) if data is not None else []


def neighbor(ip: str) -> Optional[Neighbor]:
    """ARP evidence for one address.

    ``SendARP`` asks the network directly (no admin rights needed): a reply
    gives the MAC, "nobody answered" is definite absence. Its answer may come
    from the cache, so freshness is still judged against the table read before
    the test (state STALE = "not proven fresh"). Falls back to ``arp -a <ip>``.
    """
    from ..measure.winicmp import arp_lookup

    verdict, mac = arp_lookup(ip)
    if verdict == "present":
        return Neighbor(ip=ip, mac=mac, state=NEIGH_STALE)
    if verdict == "absent":
        return Neighbor(ip=ip, mac=None, state=NEIGH_FAILED)
    res = run(["arp", "-a", ip], timeout=10)
    for n in parse_arp_a(res.stdout):
        if n.ip == ip:
            return n
    # Not listed: Windows only lists resolved entries, so ARP got no answer.
    return Neighbor(ip=ip, mac=None, state=NEIGH_INCOMPLETE) if res.found else None


def link_stats() -> Dict[str, LinkStats]:
    return parse_stats_json(
        run_powershell(_PS_PREAMBLE + _STATS_SELECT + " | ConvertTo-Json -Compress")
    )
