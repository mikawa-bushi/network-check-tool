"""L2: ARP/neighbour table — duplicate IPs, and the gateway's MAC for reference.

Two MACs answering for one IP on one link is an IP conflict, which causes
intermittent loss. Whether the gateway answers ARP is judged by the ``gateway``
check, which pings it and reads its ARP entry around the ping.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from ..core import CheckResult, Context, Probe, register
from ..i18n import Text, tr
from ..netinfo import model as nm
from ._common import oscmd


def _neighbours():
    from .. import netinfo

    return netinfo.neighbors()


@register
class NeighborProbe(Probe):
    name = "neighbor"
    title = Text("ARP表", "ARP / neighbours")
    layer = "L2"
    # After gateway, so its pings have resolved the gateway's MAC before the
    # table is read.
    depends_on = ["interface", "gateway"]
    what = Text(
        "ARP表（IPとMACの対応）でIP重複を確認し、ゲートウェイのMACを表示します。",
        "Inspects the ARP/neighbour table for duplicate IPs and shows the gateway MAC.",
    )
    why = Text(
        "IP重複（同じネットワーク上で1つのIPに複数のMAC）は断続的な通信断の原因です。",
        "A duplicate IP (two MACs for one address on one link) causes intermittent loss.",
    )
    manual_commands = ["arp -a", "ip neigh show"]

    def path_dependencies(self, ctx: Context) -> List[str]:
        return ["interface"]  # the table can be read whatever the gateway did

    def check(self, ctx: Context) -> CheckResult:
        neigh = [
            n
            for n in _neighbours()
            if not nm.mac_is_null(n.mac) or n.state in nm.NEIGH_ABSENT
        ]
        if not neigh:
            return CheckResult.skip(
                self.name,
                tr("ARP表が取得できないか空です", "neighbour table unavailable/empty"),
                code="neighbor.unavailable",
            )

        # The same IP may sit on two NICs (two networks that both use
        # 192.168.1.1); only two MACs on one link are a conflict.
        macs: Dict[Tuple[Optional[str], str], Set[str]] = defaultdict(set)
        for n in neigh:
            if n.mac:
                macs[(n.interface, n.ip)].add(n.mac)
        dups = {ip: sorted(m) for (_, ip), m in macs.items() if len(m) > 1}

        best = ctx.network().preferred_default_route()
        gw = best.gateway if best else None
        gw_mac = next((n.mac for n in neigh if n.ip == gw and n.mac), None)
        detail = {
            "neighbours": [n.to_dict() for n in neigh],
            "gateway": gw,
            "duplicates": dups,
        }
        if dups:
            ip = next(iter(dups))
            return CheckResult.fail(
                self.name,
                tr(
                    f"IP重複を検出: {ip} に複数のMAC（{', '.join(dups[ip])}）",
                    f"duplicate IP detected: {ip} maps to multiple MACs "
                    f"({', '.join(dups[ip])}) — IP conflict on the LAN",
                ),
                detail=detail,
                code="neighbor.duplicate_ip",
            )
        resolved = [n for n in neigh if n.mac]
        summary = tr(
            f"{len(resolved)} 件、IP重複なし",
            f"{len(resolved)} neighbours, no conflicts",
        )
        if gw_mac:
            summary += tr(
                f"、ゲートウェイ {gw} = {gw_mac}",
                f"; gateway {gw} at {gw_mac}",
            )
        return CheckResult.ok(self.name, summary, detail=detail, code="neighbor.ok")

    def advice(self, result, results=None):
        return (
            tr(
                "同じIPアドレスを複数の機器が使っています（IP重複）。",
                "Two devices use the same IP address on one network (IP conflict).",
            ),
            [
                tr(
                    "同じIPを使っている機器がないか確認する（交換した機器・持ち込みPCの固定IPに注意）",
                    "Look for a duplicate IP on the LAN (another device with the same address).",
                ),
                tr(
                    f"ARP表を確認: {oscmd('ip neigh show', 'arp -a', 'arp -an')}",
                    f"Inspect the ARP table: {oscmd('ip neigh show', 'arp -a', 'arp -an')}",
                ),
                tr(
                    f"ARPを消して再解決: {oscmd('sudo ip neigh flush all', 'arp -d *（管理者）', 'sudo arp -a -d')}",
                    f"Flush and re-resolve: {oscmd('sudo ip neigh flush all', 'arp -d * (admin)', 'sudo arp -a -d')}",
                ),
            ],
        )
