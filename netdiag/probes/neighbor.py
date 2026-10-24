"""L2: ARP/neighbour table — duplicate IPs and a resolvable gateway MAC.

If the gateway's IP cannot be resolved to a MAC, traffic never leaves the host
even though routing looks fine. Duplicate-IP situations cause the same flapping.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict

from ..core import CheckResult, Context, Probe, register
from ..i18n import Text, tr
from ..netinfo import model as nm
from ._common import oscmd
from .gateway import default_gateway


def _neighbours():
    from .. import netinfo

    return netinfo.neighbors()


@register
class NeighborProbe(Probe):
    name = "neighbor"
    title = Text("ARP表", "ARP / neighbours")
    layer = "L2"
    depends_on = ["interface"]
    what = Text(
        "ARP表（IPとMACの対応）でIP重複とゲートウェイのMAC解決を確認します。",
        "Inspects the ARP/neighbour table for duplicate IPs and the gateway MAC.",
    )
    why = Text(
        "ゲートウェイのMACが解決できないと経路が正しくても通信できません。IP重複（1つのIPに複数のMAC）は断続的な通信断の原因です。",
        "An unresolved gateway MAC stops all egress despite valid routing; a "
        "duplicate IP (two MACs for one address) causes intermittent loss.",
    )
    manual_commands = ["arp -a", "ip neigh show"]

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

        # Duplicate IP detection: same IP, multiple distinct MACs.
        macs_by_ip: Dict[str, set] = defaultdict(set)
        for n in neigh:
            if n.mac:
                macs_by_ip[n.ip].add(n.mac)
        dups = {ip: sorted(m) for ip, m in macs_by_ip.items() if len(m) > 1}

        gw = default_gateway()
        gw_entry = next((n for n in neigh if n.ip == gw), None)
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
                    f"duplicate IP detected: {ip} maps to multiple MACs (IP conflict on the LAN)",
                ),
                detail=detail,
                code="neighbor.duplicate_ip",
            )
        if gw and gw_entry is not None and gw_entry.state == nm.NEIGH_FAILED:
            return CheckResult.fail(
                self.name,
                tr(
                    f"ゲートウェイ {gw} のARP解決に失敗 — L2でルーターに届いていません",
                    f"gateway {gw} ARP resolution FAILED — cannot reach router at L2",
                ),
                detail=detail,
                code="neighbor.gateway_arp_failed",
            )
        if gw and (gw_entry is None or not gw_entry.mac):
            return CheckResult.warn(
                self.name,
                tr(
                    f"ゲートウェイ {gw} のARPエントリがまだありません（MAC未解決）",
                    f"gateway {gw} has no ARP entry yet (MAC unresolved)",
                ),
                detail=detail,
                code="neighbor.gateway_unresolved",
            )
        resolved = [n for n in neigh if n.mac]
        summary = tr(
            f"{len(resolved)} 件、IP重複なし",
            f"{len(resolved)} neighbours, no conflicts",
        )
        if gw_entry is not None and gw_entry.mac:
            summary += tr(
                f"、ゲートウェイ {gw} = {gw_entry.mac}",
                f"; gateway {gw} at {gw_entry.mac}",
            )
        return CheckResult.ok(self.name, summary, detail=detail, code="neighbor.ok")

    def advice(self, result, results=None):
        return (
            tr(
                "L2（ARP）の問題: IP重複、またはゲートウェイのMACが解決できません。",
                "Layer-2 problem: an IP conflict or the gateway's MAC can't be resolved.",
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
