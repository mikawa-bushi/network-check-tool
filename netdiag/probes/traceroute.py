"""L3 path analysis: where along the path do packets stop?

ping answers reachable/not; traceroute answers *where* it breaks — your own
router, the ISP's third hop, or further out — which is what decides whether the
user reboots the router or calls the ISP.
"""

from __future__ import annotations

import re
from typing import List, Optional

from ..core import CheckResult, Context, Probe, register
from ..core.util import have, is_windows, run

# Matches both traceroute ("  1  10.0.0.1 ...") and tracepath (" 1:  10.0.0.1 ...");
# the optional "?:" handles tracepath's localhost/pmtu discovery line.
_HOP_RE = re.compile(r"^\s*(\d+)\??:?\s+(.*)$")
_IP_RE = re.compile(r"(\d+\.\d+\.\d+\.\d+)")
_MS_RE = re.compile(r"([\d.]+)\s*ms")


def _parse_hops(text: str) -> List[dict]:
    # tracepath prints several lines per hop; collapse to one entry per hop
    # number, preferring a line that actually resolved an IP.
    by_hop: dict = {}
    order: List[int] = []
    for line in text.splitlines():
        m = _HOP_RE.match(line)
        if not m:
            continue
        rest = m.group(2)
        if "pmtu" in rest or "[LOCALHOST]" in rest:
            continue
        n = int(m.group(1))
        ipm = _IP_RE.search(rest)
        ms = [float(x) for x in _MS_RE.findall(rest)]
        rec = {
            "hop": n,
            "ip": ipm.group(1) if ipm else None,
            "rtt_ms": round(sum(ms) / len(ms), 2) if ms else None,
            "timeout": "*" in rest and not ipm,
        }
        if n not in by_hop:
            order.append(n)
            by_hop[n] = rec
        elif by_hop[n]["ip"] is None and rec["ip"] is not None:
            by_hop[n] = rec  # upgrade a timed-out hop to a resolved one
    return [by_hop[n] for n in order]


@register
class TracerouteProbe(Probe):
    name = "traceroute"
    title = "Path trace"
    layer = "L3"
    depends_on = ["gateway"]
    requires_root = False
    what = "Traces the hop-by-hop path to a destination and where it stops."
    why = (
        "Pinpoints the failing segment. Stopping at hop 1 implicates your router; "
        "stopping mid-path implicates a specific upstream/ISP segment."
    )
    manual_commands = ["traceroute -n -w2 -q1 <host>", "tracepath <host>"]

    def check(self, ctx: Context) -> CheckResult:
        host = ctx.get("trace_target", ctx.get("external_ip", "1.1.1.1"))
        max_hops = int(ctx.get("max_hops", 20))

        if is_windows() and have("tracert"):
            res = run(["tracert", "-d", "-h", str(max_hops), host], timeout=60)
        elif have("traceroute"):
            res = run(
                ["traceroute", "-n", "-w", "2", "-q", "1", "-m", str(max_hops), host],
                timeout=60,
            )
        elif have("tracepath"):
            res = run(["tracepath", "-n", "-m", str(max_hops), host], timeout=60)
        else:
            return CheckResult.skip(
                self.name, "no traceroute/tracepath/tracert command"
            )

        if not res.found:
            return CheckResult.skip(self.name, "traceroute command not available")

        hops = _parse_hops(res.stdout or res.stderr)
        detail = {"target": host, "hops": hops, "output": res.stdout}
        if not hops:
            return CheckResult.skip(
                self.name, "could not parse traceroute output", detail=detail
            )

        reached = any(h["ip"] == host for h in hops)
        last_ok = None
        for h in hops:
            if h["ip"] and not h["timeout"]:
                last_ok = h
        trailing_timeouts = 0
        for h in reversed(hops):
            if h["timeout"] or not h["ip"]:
                trailing_timeouts += 1
            else:
                break

        metrics = {"hop_count": float(len(hops)), "reached": 1.0 if reached else 0.0}
        if reached:
            return CheckResult.ok(
                self.name,
                f"reached {host} in {len(hops)} hops",
                metrics=metrics,
                detail=detail,
            )
        if last_ok:
            return CheckResult.warn(
                self.name,
                f"path to {host} stops after hop {last_ok['hop']} "
                f"({last_ok['ip']}); {trailing_timeouts} trailing timeout(s)",
                metrics=metrics,
                detail=detail,
            )
        return CheckResult.fail(
            self.name,
            f"no hops responded toward {host}",
            metrics=metrics,
            detail=detail,
        )
