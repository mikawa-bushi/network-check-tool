"""L1: wired link quality — negotiated speed, duplex and error counters.

On the shop floor the physical layer is the usual suspect: damaged or
unshielded cables next to inverters and servo drives, worn connectors, and
auto-negotiation mismatches (one side fixed at 100M/full, the other falling
back to half duplex). These cause *intermittent* loss that pings to a single
device rarely explain, but the NIC's own counters and negotiated mode do.
"""

from __future__ import annotations

from typing import Dict, List

from ..core import CheckResult, Context, Probe, register
from ..i18n import Text, tr
from ..netinfo import KIND_ETHERNET
from ._common import oscmd
from .interface import speed_text

# Cumulative error ratio (since boot) worth mentioning. Counters also include
# old incidents, so this is a WARN hint; live increases are judged separately
# from before/after samples around a device sweep.
ERROR_RATIO_WARN = 1e-4


@register
class LinkProbe(Probe):
    name = "link"
    title = Text("有線リンク品質", "Wired link quality")
    layer = "L1"
    depends_on: List[str] = []
    what = Text(
        "有線LANのリンク速度・全二重/半二重・NICのエラーカウンタを確認します。",
        "Reads negotiated speed, duplex and NIC error counters of wired links.",
    )
    why = Text(
        "ケーブル劣化・ノイズ・オートネゴシエーション不一致は、断続的なパケットロスとして現れます。NIC自身の記録で物理層の問題を切り分けます。",
        "Bad cables, electrical noise and auto-negotiation mismatches show up as "
        "intermittent loss; the NIC's own counters isolate the physical layer.",
    )
    manual_commands = [
        "ethtool <iface> ; ip -s link show <iface>",
        "Get-NetAdapter | ft Name,LinkSpeed,FullDuplex ; Get-NetAdapterStatistics",
    ]

    def check(self, ctx: Context) -> CheckResult:
        hn = ctx.network()
        wired = [i for i in hn.interfaces if i.kind == KIND_ETHERNET and i.up]
        if not wired:
            return CheckResult.skip(
                self.name,
                tr(
                    "リンク中の有線LANアダプタがありません",
                    "no wired interface with link",
                ),
                code="link.no_wired",
            )
        issues: List[str] = []
        codes: List[str] = []
        metrics: Dict[str, float] = {}
        per_if = []
        for i in wired:
            rec = {"name": i.name, "speed_mbps": i.speed_mbps, "duplex": i.duplex}
            if i.duplex == "half":
                issues.append(
                    tr(
                        f"{i.name}: 半二重でリンク（オートネゴシエーション不一致の疑い）",
                        f"{i.name}: half duplex (auto-negotiation mismatch?)",
                    )
                )
                codes.append("link.half_duplex")
            if i.speed_mbps is not None and i.speed_mbps <= 10:
                issues.append(
                    tr(
                        f"{i.name}: 10 Mbps でリンク（ケーブル不良・ネゴ失敗の疑い。機器が10M固定なら正常）",
                        f"{i.name}: linked at 10 Mbps (bad cable / failed negotiation, unless the device is 10M-only)",
                    )
                )
                codes.append("link.low_speed")
            st = i.stats
            if st is not None:
                rec["stats"] = st.to_dict()
                pkts = (st.rx_packets or 0) + (st.tx_packets or 0)
                errs = st.errors_total()
                if pkts and errs:
                    ratio = errs / pkts
                    metrics[f"error_ratio.{i.name}"] = ratio
                    if ratio >= ERROR_RATIO_WARN:
                        issues.append(
                            tr(
                                f"{i.name}: 起動以来のエラー {errs} 件（{ratio * 100:.3f}%）— ケーブル/コネクタ/ノイズを確認",
                                f"{i.name}: {errs} errors since boot ({ratio * 100:.3f}%) — check cable/connector/noise",
                            )
                        )
                        codes.append("link.errors")
            per_if.append(rec)
        detail = {"interfaces": per_if}
        if issues:
            return CheckResult.warn(
                self.name,
                "; ".join(issues),
                metrics=metrics,
                detail=detail,
                code=codes[0],
            )
        desc = ", ".join(
            i.name
            + (f" {speed_text(i.speed_mbps)}" if i.speed_mbps else "")
            + (tr(" 全二重", " full") if i.duplex == "full" else "")
            for i in wired
        )
        return CheckResult.ok(
            self.name,
            desc + tr("、エラーなし", ", no errors"),
            metrics=metrics,
            detail=detail,
            code="link.ok",
        )

    def advice(self, result, results=None):
        return (
            tr(
                "有線リンクの品質に問題の兆候があります（物理層）。",
                "The wired link shows physical-layer trouble.",
            ),
            [
                tr(
                    "LANケーブルを交換して再測定する（シールド付き・規格品、動力線から離す）",
                    "Swap the cable and re-test (shielded, away from power cables)",
                ),
                tr(
                    "スイッチと機器の速度/二重設定を両方『自動』に揃える（片側だけ固定は半二重化の原因）",
                    "Set speed/duplex to 'auto' on BOTH ends (fixing only one side causes half duplex)",
                ),
                tr(
                    "スイッチのポートを変えて再測定し、ポート故障を切り分ける",
                    "Move to another switch port to rule out a bad port",
                ),
                oscmd("ethtool <iface>; ip -s link", "Get-NetAdapterStatistics"),
            ],
        )
