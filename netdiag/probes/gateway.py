"""L3: default gateway discovery and reachability.

The gateway is the hinge of LAN-vs-WAN triage. If the NIC has an IP but the
gateway does not answer, the fault is local (router, LAN cabling, Wi-Fi). If the
gateway answers but external does not, the fault is upstream.
"""

from __future__ import annotations

from typing import Optional

from ..core import CheckResult, Context, Probe, register
from ..core.util import have, is_linux, is_macos, run
from .icmp import classify_ping, ping


def default_gateway() -> Optional[str]:
    if is_linux() and have("ip"):
        res = run(["ip", "route", "show", "default"])
        if res.ok:
            for line in res.stdout.splitlines():
                parts = line.split()
                if "via" in parts:
                    return parts[parts.index("via") + 1]
    if is_macos() and have("route"):
        res = run(["route", "-n", "get", "default"])
        if res.ok:
            for line in res.stdout.splitlines():
                line = line.strip()
                if line.startswith("gateway:"):
                    return line.split(":", 1)[1].strip()
    # Generic fallback via `netstat -rn`
    if have("netstat"):
        res = run(["netstat", "-rn"])
        if res.ok:
            for line in res.stdout.splitlines():
                parts = line.split()
                if parts and parts[0] in ("default", "0.0.0.0"):
                    for p in parts[1:]:
                        if p.count(".") == 3:
                            return p
    return None


@register
class GatewayProbe(Probe):
    name = "gateway"
    title = "Default gateway"
    layer = "L3"
    depends_on = ["interface"]
    what = "Finds the default gateway and pings it."
    why = (
        "Splits LAN problems from WAN problems. Gateway down with NIC up means a "
        "local fault (router/cable/Wi-Fi); gateway up isolates the fault upstream."
    )
    manual_commands = ["ip route show default", "ping -c4 <gateway-ip>"]

    def check(self, ctx: Context) -> CheckResult:
        gw = default_gateway()
        if not gw:
            return CheckResult.fail(
                self.name,
                "no default gateway configured (no route off-LAN)",
                metrics={"has_gateway": 0.0},
            )
        ctx.config.setdefault("_gateway", gw)
        if not have("ping"):
            return CheckResult.warn(
                self.name,
                f"default gateway {gw} (cannot ping: no ping command)",
                detail={"gateway": gw},
                metrics={"has_gateway": 1.0},
            )
        rtt, loss, text = ping(gw, count=int(ctx.get("count", 4)))
        return classify_ping(
            self.name,
            rtt,
            loss,
            text,
            fail_msg=f"default gateway {gw} not responding (100% loss) — router/LAN problem",
            warn_msg=lambda: f"gateway {gw} reachable but {loss:.0f}% loss ({rtt:.1f}ms)",
            ok_msg=lambda: f"{gw} reachable ({rtt:.2f}ms)",
            extra_metrics={"has_gateway": 1.0},
            extra_detail={"gateway": gw},
        )
