"""L3: compare IPv4 vs IPv6 reachability to the same destination.

Single-stack failures (IPv6 broken while IPv4 works, or vice versa) are common
and confusing because some apps prefer one family. Measuring both at once makes
the asymmetry obvious.
"""

from __future__ import annotations

import socket
from typing import List, Optional

from ..core import CheckResult, Context, Probe, register
from ..core.util import have, is_windows, run


def _ping_family(host: str, v6: bool, count: int = 2) -> Optional[bool]:
    cmd = "ping"
    if is_windows():
        args = [cmd, "-6" if v6 else "-4", "-n", str(count), host]
    else:
        # Linux/mac: ping handles -4/-6
        args = [cmd, "-6" if v6 else "-4", "-c", str(count), "-W", "2", host]
    if not have(cmd):
        return None
    res = run(args, timeout=count * 3 + 5)
    if not res.found:
        return None
    if "0% packet loss" in res.stdout or " 0.0% packet loss" in res.stdout:
        return True
    if "100% packet loss" in res.stdout or "100.0% packet loss" in res.stdout:
        return False
    return res.ok


def _has_family(host: str, family: int) -> bool:
    try:
        socket.getaddrinfo(host, None, family)
        return True
    except socket.gaierror:
        return False


@register
class DualStackProbe(Probe):
    name = "dualstack"
    title = "IPv4/IPv6 parity"
    layer = "L3"
    depends_on = ["dns"]
    what = "Pings the same hostname over IPv4 and IPv6 and compares."
    why = (
        "One broken address family while the other works produces app-specific, "
        "intermittent failures. Comparing both surfaces the asymmetry directly."
    )
    manual_commands = ["ping -4 -c2 <host>", "ping -6 -c2 <host>"]

    def check(self, ctx: Context) -> CheckResult:
        host = ctx.get("dns_name", "example.com")
        has4 = _has_family(host, socket.AF_INET)
        has6 = _has_family(host, socket.AF_INET6)
        v4 = _ping_family(host, v6=False) if has4 else None
        v6 = _ping_family(host, v6=True) if has6 else None
        detail = {"host": host, "ipv4_ok": v4, "ipv6_ok": v6,
                  "has_a": has4, "has_aaaa": has6}
        metrics = {}
        if v4 is not None:
            metrics["ipv4_ok"] = 1.0 if v4 else 0.0
        if v6 is not None:
            metrics["ipv6_ok"] = 1.0 if v6 else 0.0

        if v4 and v6:
            return CheckResult.ok(self.name, f"{host}: IPv4 and IPv6 both reachable",
                                  metrics=metrics, detail=detail)
        if v4 and v6 is False:
            return CheckResult.warn(
                self.name,
                f"{host}: IPv4 OK but IPv6 unreachable (apps preferring v6 will stall)",
                metrics=metrics, detail=detail)
        if v6 and v4 is False:
            return CheckResult.warn(
                self.name, f"{host}: IPv6 OK but IPv4 unreachable",
                metrics=metrics, detail=detail)
        if v4 is None and v6 is None:
            return CheckResult.skip(self.name, "no ping or no addresses to test")
        return CheckResult.fail(self.name, f"{host}: neither IPv4 nor IPv6 reachable",
                                metrics=metrics, detail=detail)
