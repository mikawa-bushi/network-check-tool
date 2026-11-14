"""L3: sanity of this PC's IP configuration.

Engineering laptops are the classic source of "it worked yesterday" problems:
the office Wi-Fi and the machine's Ethernet both on 192.168.1.0/24, a default
gateway typed into the equipment NIC, a mask of 255.0.0.0, or an address that
Windows already flagged as a duplicate. Each of these breaks traffic to some
devices while everything *looks* configured.
"""

from __future__ import annotations

import ipaddress
from itertools import combinations
from typing import List

from ..core import CheckResult, Context, Probe, register
from ..i18n import Text, tr
from ..netinfo import KIND_LOOPBACK, HostNetwork, Route
from ._common import oscmd

# A mask broader than the private block itself is always a typo
# (e.g. 192.168.3.10 with 255.0.0.0 makes all of 192.x look on-link).
_PRIVATE_MIN_PREFIX = [
    (ipaddress.ip_network("10.0.0.0/8"), 8),
    (ipaddress.ip_network("172.16.0.0/12"), 12),
    (ipaddress.ip_network("192.168.0.0/16"), 16),
]


def _same_route(hn: HostNetwork, a: Route, b: Route) -> bool:
    """Two next hops of one multipath (ECMP) default, which share the traffic.

    Linux lists such a route once per next hop, each with the route's table and
    metric. Windows and macOS keep separate defaults with equal metrics, so
    there they stay rival gateways.
    """
    return hn.source.startswith("linux") and (a.table, a.metric) == (
        b.table,
        b.metric,
    )


@register
class IpConfigProbe(Probe):
    name = "ipconfig"
    title = Text("IP設定の整合性", "IP configuration")
    layer = "L3"
    depends_on = ["interface"]
    what = Text(
        "IP重複・サブネットの重なり・複数のデフォルトゲートウェイ・誤ったサブネットマスクを確認します。",
        "Looks for duplicate IPs, overlapping subnets, multiple default gateways and broken masks.",
    )
    why = Text(
        "設定ミスがあると一部の機器にだけ届かない状態になり、ケーブルや機器の故障と区別がつきにくくなります。",
        "Misconfiguration breaks traffic to *some* devices and is easily mistaken for a cable or device fault.",
    )
    manual_commands = ["ipconfig /all", "route print -4", "ip -br addr ; ip route"]

    def check(self, ctx: Context) -> CheckResult:
        hn = ctx.network()
        fails: List[str] = []
        warns: List[str] = []
        codes: List[str] = []

        for i in hn.interfaces:
            for a in i.ipv4:
                if "duplicate" in a.flags:
                    fails.append(
                        tr(
                            f"{i.name} の {a.ip} は他の機器と重複しています（OSがIP競合を検出）",
                            f"{a.ip} on {i.name} conflicts with another device (OS detected an IP conflict)",
                        )
                    )
                    codes.append("ipconfig.duplicate_ip")
                elif "tentative" in a.flags:
                    warns.append(
                        tr(
                            f"{i.name} の {a.ip} は確認中（重複検査中）",
                            f"{a.ip} on {i.name} is still tentative",
                        )
                    )
                    codes.append("ipconfig.tentative")
                if a.is_loopback or a.is_link_local:
                    continue
                ip = ipaddress.ip_address(a.ip)
                for block, min_prefix in _PRIVATE_MIN_PREFIX:
                    if ip in block and a.prefixlen < min_prefix:
                        warns.append(
                            tr(
                                f"{i.name} {a.cidr}: サブネットマスクが広すぎます（/{min_prefix}以上が正しい範囲）",
                                f"{i.name} {a.cidr}: subnet mask too broad (expected /{min_prefix} or longer)",
                            )
                        )
                        codes.append("ipconfig.broad_mask")

        # The same subnet on two different NICs: which one carries the traffic?
        lan = [i for i in hn.lan_interfaces() if i.kind != KIND_LOOPBACK]
        seen = []
        for i in lan:
            for a in i.usable_ipv4:
                for other_if, other_a in seen:
                    if other_if.name != i.name and a.network.overlaps(other_a.network):
                        warns.append(
                            tr(
                                f"{other_if.name} ({other_a.cidr}) と {i.name} ({a.cidr}) のネットワークが重なっています — どちらのNICから送信されるか不定",
                                f"{other_if.name} ({other_a.cidr}) and {i.name} ({a.cidr}) overlap — traffic may leave through the wrong NIC",
                            )
                        )
                        codes.append("ipconfig.overlap")
                seen.append((i, a))

        defaults = hn.default_routes()
        if any(
            a.interface != b.interface and not _same_route(hn, a, b)
            for a, b in combinations(defaults, 2)
        ):
            best = hn.preferred_default_route()
            listing = ", ".join(f"{r.gateway} ({r.interface})" for r in defaults)
            warns.append(
                tr(
                    f"デフォルトゲートウェイが複数のNICにあります: {listing}。通常は {best.gateway} が使われます — 設備側NICのゲートウェイは空欄が基本",
                    f"default gateways on several NICs: {listing}; {best.gateway} normally wins — "
                    f"equipment-side NICs usually need no gateway",
                )
            )
            codes.append("ipconfig.multi_gateway")

        detail = {
            "default_routes": [r.to_dict() for r in defaults],
            "lan": [
                {"name": i.name, "addresses": [a.cidr for a in i.usable_ipv4]}
                for i in lan
            ],
        }
        if fails:
            return CheckResult.fail(
                self.name, "; ".join(fails + warns), detail=detail, code=codes[0]
            )
        if warns:
            return CheckResult.warn(
                self.name, "; ".join(warns), detail=detail, code=codes[0]
            )
        return CheckResult.ok(
            self.name,
            tr("IP設定に矛盾はありません", "no conflicting IP settings"),
            detail=detail,
            code="ipconfig.ok",
        )

    def advice(self, result, results=None):
        code = result.code if result is not None else ""
        if code == "ipconfig.duplicate_ip":
            return (
                tr(
                    "このPCのIPアドレスが他の機器と重複しています。",
                    "This PC's IP address is used by another device.",
                ),
                [
                    tr(
                        "このPCに未使用のIPを設定し直す",
                        "Re-assign this PC an unused IP",
                    ),
                    tr(
                        "重複相手は 'netdiag scan' や ARP 表 (arp -a) で探せます",
                        "Find the other device with 'netdiag scan' or the ARP table (arp -a)",
                    ),
                ],
            )
        if code == "ipconfig.overlap":
            return (
                tr(
                    "複数のNICが同じネットワークに属しています。",
                    "Several NICs share one subnet.",
                ),
                [
                    tr(
                        "設備に接続するときは、社内LAN（Wi-Fi）を一時的に切断するか、設備側を別のサブネットにする",
                        "Disconnect the office LAN/Wi-Fi while working on the equipment, or use a different subnet",
                    ),
                    oscmd(
                        "ip route get " + tr("<機器IP>", "<device-ip>"),
                        "route print -4",
                    ),
                ],
            )
        if code == "ipconfig.multi_gateway":
            return (
                tr(
                    "デフォルトゲートウェイが複数あり、通信経路が不定です。",
                    "Multiple default gateways make routing unpredictable.",
                ),
                [
                    tr(
                        "設備側NICのIPv4設定から『デフォルトゲートウェイ』を削除する（別セグメントへは静的ルートで）",
                        "Remove the default gateway from the equipment-side NIC (use static routes for other segments)",
                    ),
                    oscmd("ip route", "route print -4"),
                ],
            )
        if code == "ipconfig.broad_mask":
            return (
                tr(
                    "サブネットマスクの設定ミスの可能性があります。",
                    "The subnet mask looks mistyped.",
                ),
                [
                    tr(
                        "多くの設備LANは 255.255.255.0 (/24) です。機器のマスクと揃える",
                        "Most equipment LANs use 255.255.255.0 (/24); match the devices",
                    )
                ],
            )
        return (
            tr(
                "IP設定に確認が必要な点があります。",
                "The IP configuration needs attention.",
            ),
            [
                oscmd(
                    "ip -br addr; ip route",
                    "ipconfig /all ; route print -4",
                    "ifconfig; netstat -rn",
                )
            ],
        )
