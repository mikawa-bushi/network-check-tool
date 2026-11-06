"""L3: default gateway discovery and reachability.

The gateway is the hinge of LAN-vs-WAN triage. If the NIC has an IP but the
gateway does not answer, the fault is local (router, LAN cabling, Wi-Fi). If the
gateway answers but external does not, the fault is upstream.

Closed equipment networks often have *no* gateway by design; that is reported
as "not applicable" unless the run expects internet access.
"""

from __future__ import annotations

from typing import List, Optional

from ..core import CheckResult, Context, Probe, register
from ..i18n import Text, tr
from ..measure import ping
from ._common import classify_latency, oscmd, ping_settings, ping_summary


def default_gateway() -> Optional[str]:
    """The preferred default gateway of this host (lowest metric), if any."""
    from .. import netinfo

    best = netinfo.snapshot().preferred_default_route()
    return best.gateway if best else None


@register
class GatewayProbe(Probe):
    name = "gateway"
    title = Text("デフォルトゲートウェイ", "Default gateway")
    layer = "L3"
    depends_on = ["interface"]
    what = Text(
        "デフォルトゲートウェイ（ルーター）を調べ、pingで応答を確認します。",
        "Finds the default gateway and pings it.",
    )
    why = Text(
        "LAN内の問題とその先の問題を切り分けます。ゲートウェイが応答しなければ手元（ルーター/ケーブル/Wi-Fi）の問題です。",
        "Splits LAN problems from WAN problems. Gateway down with NIC up means a "
        "local fault (router/cable/Wi-Fi); gateway up isolates the fault upstream.",
    )
    manual_commands = ["route print -4 / ip route show default", "ping <gateway-ip>"]

    def check(self, ctx: Context) -> CheckResult:
        hn = ctx.network()
        routes = hn.default_routes()
        if not routes:
            if ctx.get("internet", True):
                return CheckResult.fail(
                    self.name,
                    tr(
                        "デフォルトゲートウェイが設定されていません（LAN外へ出る経路なし）",
                        "no default gateway configured (no route off-LAN)",
                    ),
                    metrics={"has_gateway": 0.0},
                    code="gateway.none",
                )
            return CheckResult.skip(
                self.name,
                tr(
                    "デフォルトゲートウェイなし（閉じた設備ネットワークでは正常）",
                    "no default gateway (normal for a closed equipment network)",
                ),
                metrics={"has_gateway": 0.0},
                code="gateway.none_closed",
            )
        best = hn.preferred_default_route()
        gw = best.gateway
        detail = {"gateway": gw, "interface": best.interface}
        st = ping(gw, **ping_settings(ctx.get))
        detail["ping"] = st.to_dict()
        metrics = {"has_gateway": 1.0, **st.to_metrics()}
        if st.error:
            return CheckResult.warn(
                self.name,
                tr(
                    f"ゲートウェイ {gw}（ping不可: {st.error}）",
                    f"default gateway {gw} (cannot ping: {st.error})",
                ),
                metrics={"has_gateway": 1.0},
                detail=detail,
                code="gateway.unmeasured",
            )
        if not st.any_reply:
            return CheckResult.fail(
                self.name,
                tr(
                    f"ゲートウェイ {gw} が応答しません（{ping_summary(st)}）— ルーター/LAN側の問題",
                    f"default gateway {gw} not responding ({ping_summary(st)}) — router/LAN problem",
                ),
                metrics=metrics,
                detail=detail,
                code="gateway.unreachable",
            )
        slow = classify_latency(st, on_link=True)
        if st.lost or st.duplicates or slow:
            reason = slow or tr("パケットロスあり", "packet loss")
            return CheckResult.warn(
                self.name,
                tr(
                    f"ゲートウェイ {gw}: {reason}（{ping_summary(st)}）",
                    f"gateway {gw}: {reason} ({ping_summary(st)})",
                ),
                metrics=metrics,
                detail=detail,
                code="gateway.degraded",
            )
        return CheckResult.ok(
            self.name,
            f"{gw} ({ping_summary(st)})",
            metrics=metrics,
            detail=detail,
            code="gateway.ok",
        )

    def advice(self, result, results=None):
        gw = (result.detail.get("gateway") if result is not None else None) or tr(
            "<ゲートウェイIP>", "<gateway-ip>"
        )
        if result is not None and result.code == "gateway.none":
            return (
                tr(
                    "デフォルトゲートウェイが無いため、LANの外へ出られません。",
                    "There is no default gateway, so nothing beyond the LAN is reachable.",
                ),
                [
                    tr(
                        "LAN外（社内サーバー・インターネット）へ出る必要がある場合のみ、ネットワーク管理者に確認してゲートウェイを設定する",
                        "Only if the PC must reach other networks: ask the network admin for the gateway address",
                    ),
                    oscmd("ip route", "route print -4", "netstat -rn"),
                ],
            )
        return (
            tr(
                f"ゲートウェイ（ルーター）{gw} が応答しません — LAN内またはルーターの問題です。",
                f"The default gateway {gw} is not responding — a local/LAN or router fault.",
            ),
            [
                tr(
                    f"同じLANの別のPCから {gw} にpingする。別PCもNGならルーター側、OKならこのPC側の問題",
                    f"From another device on the same LAN, ping {gw}. If that also fails, "
                    f"the router is the problem; if it works, the fault is this host.",
                ),
                tr(
                    "ルーター/アクセスポイントの電源・ランプを確認し、必要なら再起動する",
                    "Check / reboot the router or access point",
                ),
                tr(
                    f"経路を確認: {oscmd('ip route show default', 'route print -4', 'netstat -rn')}",
                    f"Confirm the route: {oscmd('ip route show default', 'route print -4', 'netstat -rn')}",
                ),
            ],
        )


__all__: List[str] = ["GatewayProbe", "default_gateway"]
