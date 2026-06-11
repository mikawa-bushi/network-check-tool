"""L2/L3 base: is there a working network interface with an IP address?

This is the bottom of the dependency graph. If no interface is up with an IPv4
address, every higher-layer failure (gateway, DNS, HTTP) is a consequence, not a
cause, and the engine will fold them away.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Dict, List, Optional, Tuple

from ..core import CheckResult, Context, Probe, Status, register
from ..core.util import have, is_linux, is_macos, run


def _primary_ip() -> Optional[str]:
    """Best-effort local IP used for the default route, without sending traffic."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 203.0.113.0/24 (TEST-NET-3) — never actually routed/sent for UDP connect.
        s.connect(("203.0.113.1", 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _linux_interfaces() -> List[Dict]:
    out: List[Dict] = []
    res = run(["ip", "-o", "addr", "show"])
    if not res.ok:
        return out
    # lines like: "2: eth0    inet 192.168.1.10/24 brd ... scope global eth0"
    by_if: Dict[str, Dict] = {}
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        ifname = parts[1]
        rec = by_if.setdefault(ifname, {"name": ifname, "addrs": []})
        if "inet" in parts:
            i = parts.index("inet")
            if i + 1 < len(parts):
                rec["addrs"].append(parts[i + 1])
        elif "inet6" in parts:
            i = parts.index("inet6")
            if i + 1 < len(parts):
                rec["addrs"].append(parts[i + 1])
    # link state
    link = run(["ip", "-o", "link", "show"])
    states: Dict[str, bool] = {}
    if link.ok:
        for line in link.stdout.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            name = parts[1].rstrip(":")
            states[name] = "state UP" in line or "LOWER_UP" in line
    for name, rec in by_if.items():
        rec["up"] = states.get(name, False)
        out.append(rec)
    return out


@register
class InterfaceProbe(Probe):
    name = "interface"
    title = "Network interface"
    layer = "L2"
    depends_on: List[str] = []
    what = "Checks that a non-loopback interface is UP and holds an IPv4 address."
    why = (
        "Without a link-up interface and an assigned address there is no way to "
        "send any packet; every higher-layer failure below this is a symptom."
    )
    manual_commands = ["ip -o addr show", "ip -o link show"]

    def check(self, ctx: Context) -> CheckResult:
        ip = _primary_ip()
        ifaces = _linux_interfaces() if (is_linux() and have("ip")) else []

        if not is_linux() or not have("ip"):
            # Fall back to the socket probe only.
            if ip:
                return CheckResult.ok(
                    self.name,
                    f"local IPv4 {ip} (interface enumeration unavailable on this platform)",
                    metrics={"has_ip": 1.0},
                    detail={"primary_ip": ip},
                )
            return CheckResult.fail(
                self.name,
                "no usable local IPv4 address found",
                metrics={"has_ip": 0.0},
            )

        usable = [
            f for f in ifaces
            if f["name"] != "lo" and f["up"] and any(
                self._is_global_v4(a) for a in f["addrs"]
            )
        ]
        if not usable:
            up_but_no_ip = [f["name"] for f in ifaces if f["name"] != "lo" and f["up"]]
            if up_but_no_ip:
                return CheckResult.fail(
                    self.name,
                    f"interface(s) {', '.join(up_but_no_ip)} are UP but have no IPv4 "
                    f"address (DHCP failure or unplugged after link-up?)",
                    metrics={"has_ip": 0.0},
                    detail={"interfaces": ifaces},
                )
            return CheckResult.fail(
                self.name,
                "no interface is UP with an IPv4 address",
                metrics={"has_ip": 0.0},
                detail={"interfaces": ifaces},
            )

        names = ", ".join(
            f"{f['name']} {','.join(a for a in f['addrs'] if self._is_global_v4(a))}"
            for f in usable
        )
        return CheckResult.ok(
            self.name,
            names,
            metrics={"has_ip": 1.0, "interface_count": float(len(usable))},
            detail={"interfaces": ifaces, "primary_ip": ip},
        )

    @staticmethod
    def _is_global_v4(cidr: str) -> bool:
        try:
            addr = ipaddress.ip_interface(cidr).ip
            return addr.version == 4 and not addr.is_loopback and not addr.is_link_local
        except ValueError:
            return False
