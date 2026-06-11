"""L2: ARP/neighbour table — duplicate IPs and a resolvable gateway MAC.

If the gateway's IP cannot be resolved to a MAC, traffic never leaves the host
even though routing looks fine. Duplicate-IP situations cause the same flapping.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List

from ..core import CheckResult, Context, Probe, register
from ..core.util import have, is_linux, run
from .gateway import default_gateway


def _neighbours() -> List[Dict]:
    out: List[Dict] = []
    if is_linux() and have("ip"):
        res = run(["ip", "neigh", "show"])
        # "192.168.1.1 dev eth0 lladdr aa:bb:cc:dd:ee:ff REACHABLE"
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            rec = {"ip": parts[0], "mac": None, "state": parts[-1]}
            if "lladdr" in parts:
                rec["mac"] = parts[parts.index("lladdr") + 1]
            out.append(rec)
    elif have("arp"):
        res = run(["arp", "-an"])
        for line in res.stdout.splitlines():
            m = re.search(r"\(([\d.]+)\) at ([0-9a-f:]+)", line)
            if m:
                out.append({"ip": m.group(1), "mac": m.group(2), "state": "?"})
    return out


@register
class NeighborProbe(Probe):
    name = "neighbor"
    title = "ARP / neighbours"
    layer = "L2"
    depends_on = ["interface"]
    what = "Inspects the ARP/neighbour table for duplicate IPs and the gateway MAC."
    why = (
        "An unresolved gateway MAC stops all egress despite valid routing; a "
        "duplicate IP (two MACs for one address) causes intermittent loss."
    )
    manual_commands = ["ip neigh show", "arp -an"]

    def check(self, ctx: Context) -> CheckResult:
        neigh = _neighbours()
        if not neigh:
            return CheckResult.skip(self.name, "neighbour table unavailable/empty")

        # Duplicate IP detection: same IP, multiple distinct MACs.
        macs_by_ip: Dict[str, set] = defaultdict(set)
        for n in neigh:
            if n.get("mac"):
                macs_by_ip[n["ip"]].add(n["mac"])
        dups = {ip: list(m) for ip, m in macs_by_ip.items() if len(m) > 1}

        gw = default_gateway()
        gw_entry = next((n for n in neigh if n["ip"] == gw), None)
        detail = {"neighbours": neigh, "gateway": gw, "duplicates": dups}

        if dups:
            return CheckResult.fail(
                self.name,
                f"duplicate IP detected: {list(dups)[0]} maps to multiple MACs "
                f"(IP conflict on the LAN)",
                detail=detail,
            )
        if gw and gw_entry is None:
            return CheckResult.warn(
                self.name,
                f"gateway {gw} has no ARP entry yet (MAC unresolved)",
                detail=detail,
            )
        if gw and gw_entry and gw_entry.get("state") == "FAILED":
            return CheckResult.fail(
                self.name,
                f"gateway {gw} ARP resolution FAILED — cannot reach router at L2",
                detail=detail,
            )
        summary = f"{len(neigh)} neighbours, no conflicts"
        if gw_entry and gw_entry.get("mac"):
            summary += f"; gateway {gw} at {gw_entry['mac']}"
        return CheckResult.ok(self.name, summary, detail=detail)
