"""L3: compare IPv4 vs IPv6 reachability to the same destination.

Single-stack failures (IPv6 broken while IPv4 works, or vice versa) are common
and confusing because some apps prefer one family. Measuring both at once makes
the asymmetry obvious.
"""

from __future__ import annotations

import socket
from typing import Optional

from ..core import CheckResult, Context, Probe, register
from ..core.util import have
from .icmp import ping


def _ping_family(host: str, v6: bool, count: int = 2) -> Optional[bool]:
    """Ping ``host`` over one IP family. True=reachable, False=down, None=untested.

    Delegates execution and parsing to ``icmp.ping`` (with ``-4``/``-6``) instead
    of re-implementing the arg builder and a brittle ``"0% packet loss" in stdout``
    substring match -- the latter mis-reads e.g. "10% packet loss" as success.
    """
    if not have("ping"):
        return None
    _rtt, loss, text = ping(host, count=count, family=6 if v6 else 4)
    if not text:
        return None  # ping produced no output (could not run for this family)
    return loss < 100.0


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
        # Honour the shared ``count`` (other ping probes do); keep dual-stack's
        # lighter default of 2 when the user hasn't asked for more.
        count = int(ctx.get("count", 2))
        has4 = _has_family(host, socket.AF_INET)
        has6 = _has_family(host, socket.AF_INET6)
        v4 = _ping_family(host, v6=False, count=count) if has4 else None
        v6 = _ping_family(host, v6=True, count=count) if has6 else None
        detail = {
            "host": host,
            "ipv4_ok": v4,
            "ipv6_ok": v6,
            "has_a": has4,
            "has_aaaa": has6,
        }
        metrics = {}
        if v4 is not None:
            metrics["ipv4_ok"] = 1.0 if v4 else 0.0
        if v6 is not None:
            metrics["ipv6_ok"] = 1.0 if v6 else 0.0

        if v4 and v6:
            return CheckResult.ok(
                self.name,
                f"{host}: IPv4 and IPv6 both reachable",
                metrics=metrics,
                detail=detail,
            )
        if v4 and v6 is False:
            return CheckResult.warn(
                self.name,
                f"{host}: IPv4 OK but IPv6 unreachable (apps preferring v6 will stall)",
                metrics=metrics,
                detail=detail,
            )
        if v6 and v4 is False:
            return CheckResult.warn(
                self.name,
                f"{host}: IPv6 OK but IPv4 unreachable",
                metrics=metrics,
                detail=detail,
            )
        if v4 is None and v6 is None:
            return CheckResult.skip(self.name, "no ping or no addresses to test")
        return CheckResult.fail(
            self.name,
            f"{host}: neither IPv4 nor IPv6 reachable",
            metrics=metrics,
            detail=detail,
        )
