"""L3: default gateway discovery and reachability.

The gateway is the hinge of LAN-vs-WAN triage. If the NIC has an IP but the
gateway does not answer, the fault is local (router, LAN cabling, Wi-Fi). If the
gateway answers but external does not, the fault is upstream.

Many routers and firewalls ignore ping to themselves, so a gateway that does
not answer ping but answers ARP, or forwards traffic, is present: a warning,
never a failure that hides the internet checks. Closed equipment networks often
have *no* gateway by design; that is reported as "not applicable", and a
gateway typed in there that does not answer is a warning.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .. import netinfo
from ..core import CheckResult, Context, Probe, register
from ..i18n import Text, tr
from ..measure import PingStats, ping, tcp_connect_once
from ..netinfo import Neighbor
from ._common import classify_latency, oscmd, ping_settings, ping_summary
from .device import l2_evidence


def default_gateway() -> Optional[str]:
    """The preferred default gateway of this host (lowest metric), if any."""
    best = netinfo.snapshot().preferred_default_route()
    return best.gateway if best else None


def _forwards(host: str) -> bool:
    """One TCP connection off the LAN succeeds, so the gateway forwards traffic."""
    return tcp_connect_once(host, 443, timeout=2.0)[0]


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
        if not hn.routes and hn.errors:
            return CheckResult.unknown(
                self.name,
                tr("経路表を読み取れませんでした", "could not read the routes"),
                detail={"errors": list(hn.errors)},
                code="gateway.routes_unknown",
            )
        best = hn.preferred_default_route()
        if best is None:
            return self._no_gateway(ctx)
        gw = best.gateway
        detail: Dict[str, Any] = {
            "gateway": gw,
            "interface": best.interface,
            "other_gateways": [
                f"{r.gateway} ({r.interface})"
                for r in hn.default_routes()
                if r.interface != best.interface
            ],
        }
        before = netinfo.neighbor(gw)
        started = time.monotonic()
        st = ping(gw, **ping_settings(ctx.get))
        detail["ping"] = st.to_dict()
        metrics = {"has_gateway": 1.0, **st.to_metrics()}
        if st.error:
            return CheckResult.unknown(
                self.name,
                tr(
                    f"ゲートウェイ {gw} にpingできません（{st.error}）",
                    f"cannot ping the default gateway {gw} ({st.error})",
                ),
                metrics={"has_gateway": 1.0},
                detail=detail,
                code="gateway.unmeasured",
            )
        if not st.any_reply:
            window = max(time.monotonic() - started, 1.0)
            return self._silent(ctx, st, before, window, detail, metrics)
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

    def _no_gateway(self, ctx: Context) -> CheckResult:
        link = ctx.network().link_default_route()
        if link is not None:
            return CheckResult.skip(
                self.name,
                tr(
                    f"LAN外への通信は {link.interface} から直接出ます（pingするルーターはありません）",
                    f"traffic leaves via {link.interface}, no router to ping",
                ),
                detail={"interface": link.interface},
                code="gateway.via_link",
            )
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

    def _silent(
        self,
        ctx: Context,
        st: PingStats,
        before: Optional[Neighbor],
        window: float,
        detail: Dict[str, Any],
        metrics: Dict[str, float],
    ) -> CheckResult:
        """No ping reply: ask ARP, then traffic through it, whether it is there."""
        gw, iface = detail["gateway"], detail["interface"]
        internet = ctx.get("internet", True)
        after = netinfo.neighbor(gw)
        l2 = l2_evidence(before, after, window)
        detail["l2"] = l2
        detail["mac"] = after.mac if after is not None else None
        present = ""
        if l2 == "present":
            present = tr("ARPには応答", "it answers ARP")
        elif l2 == "unknown" and internet:
            target = ctx.get("external_ip", "1.1.1.1")
            detail["forwards"] = _forwards(target)
            if detail["forwards"]:
                present = tr(
                    f"経由する通信（{target}:443）は成功",
                    f"traffic through it ({target}:443) works",
                )
        if present:
            return CheckResult.warn(
                self.name,
                tr(
                    f"ゲートウェイ {gw} はpingに応答しませんが存在します（{present}）— ICMP無効/フィルタの可能性",
                    f"default gateway {gw} does not answer ping, but is present ({present}) — ICMP disabled or filtered?",
                ),
                metrics=metrics,
                detail=detail,
                code="gateway.icmp_blocked",
            )
        no_arp = (
            tr("、ARPにも応答なし", ", no ARP reply either") if l2 == "absent" else ""
        )
        if internet:
            return CheckResult.fail(
                self.name,
                tr(
                    f"ゲートウェイ {gw} が応答しません（{ping_summary(st)}{no_arp}）— ルーター/LAN側の問題",
                    f"default gateway {gw} not responding ({ping_summary(st)}{no_arp}) — router/LAN problem",
                ),
                metrics=metrics,
                detail=detail,
                code="gateway.unreachable",
            )
        return CheckResult.warn(
            self.name,
            tr(
                f"{iface} のゲートウェイ {gw} が応答しません（{ping_summary(st)}{no_arp}）— 閉じた設備ネットワークではゲートウェイは不要です",
                f"the gateway {gw} set on {iface} does not answer ({ping_summary(st)}{no_arp}); "
                f"a closed equipment network needs no gateway",
            ),
            metrics=metrics,
            detail=detail,
            code="gateway.unreachable_closed",
        )

    def advice(self, result, results=None):
        code = result.code if result is not None else ""
        detail = result.detail if result is not None else {}
        gw = detail.get("gateway") or tr("<ゲートウェイIP>", "<gateway-ip>")
        iface = detail.get("interface") or tr("<アダプタ>", "<adapter>")
        others = ", ".join(detail.get("other_gateways") or [])
        show_route = oscmd("ip route show default", "route print -4", "netstat -rn")
        if code == "gateway.none":
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
        if code == "gateway.routes_unknown":
            return (
                tr(
                    "このPCの経路表を読み取れなかったため、ゲートウェイを確認していません。",
                    "This PC's routing table could not be read, so the gateway was not checked.",
                ),
                [
                    tr(
                        f"経路を手で確認: {show_route}",
                        f"Check the routes by hand: {show_route}",
                    )
                ],
            )
        if code == "gateway.unmeasured":
            return (
                tr(
                    f"このPCではpingを実行できないため、ゲートウェイ {gw} の応答は測っていません。",
                    f"ping cannot run on this PC, so the gateway {gw} was not measured.",
                ),
                [tr(f"手でpingする: ping {gw}", f"Ping it by hand: ping {gw}")],
            )
        if code == "gateway.icmp_blocked":
            return (
                tr(
                    f"ゲートウェイ {gw} は存在しますが、pingには応答しません。ルーター/ファイアウォールの設定でpingが無効になっていることが多く、それ自体は故障ではありません。",
                    f"The gateway {gw} is present but does not answer ping. Routers and "
                    f"firewalls often have ping disabled; that alone is not a fault.",
                ),
                [
                    tr(
                        "ゲートウェイの遅延やロスを測りたい場合は、ネットワーク管理者にpingの許可を相談する",
                        "To measure the gateway's latency and loss, ask the network admin to allow ping to it",
                    ),
                    tr(
                        f"ARPを確認: {oscmd(f'ip neigh show {gw}', f'arp -a {gw}', f'arp -n {gw}')}",
                        f"Check its ARP entry: {oscmd(f'ip neigh show {gw}', f'arp -a {gw}', f'arp -n {gw}')}",
                    ),
                ],
            )
        if code == "gateway.degraded":
            return (
                tr(
                    f"ゲートウェイ {gw} は応答しますが、ロスや遅延があります — このPCからルーターまでのLAN/Wi-Fiが不安定です。",
                    f"The gateway {gw} answers, but with loss or delay — the LAN or Wi-Fi "
                    f"between this PC and the router is unstable.",
                ),
                [
                    tr(
                        "ケーブル・スイッチのポート、Wi-Fiの電波強度を確認する",
                        "Check the cable and switch port, or the Wi-Fi signal",
                    ),
                    tr(
                        f"長めに測って傾向を見る: netdiag soak {gw}",
                        f"Measure for longer to see the pattern: netdiag soak {gw}",
                    ),
                ],
            )
        if others and code in ("gateway.unreachable", "gateway.unreachable_closed"):
            return (
                tr(
                    f"このPCは {iface} のゲートウェイ {gw} を使いますが、応答しません。他のNICにもゲートウェイ（{others}）があります。設備側NICに入力したゲートウェイが優先されていることがよくあります。",
                    f"This PC sends off-LAN traffic to {gw} on {iface}, which does not "
                    f"answer, while other NICs have gateways too ({others}). A gateway "
                    f"typed into the equipment NIC often wins like this.",
                ),
                [
                    tr(
                        f"{iface} のIPv4設定から『デフォルトゲートウェイ』を削除する（設備のネットワークにゲートウェイは不要）",
                        f"Remove the default gateway from {iface}'s IPv4 settings "
                        f"(the equipment network needs none)",
                    ),
                    tr(f"経路を確認: {show_route}", f"Confirm the route: {show_route}"),
                ],
            )
        if code == "gateway.unreachable_closed":
            return (
                tr(
                    f"{iface} にゲートウェイ {gw} が設定されていますが、その機器は応答しません。閉じた設備ネットワークではゲートウェイは不要です。",
                    f"{iface} has the gateway {gw} set, but nothing answers there. A "
                    f"closed equipment network needs no gateway.",
                ),
                [
                    tr(
                        f"LAN外へ出る必要がなければ、{iface} のゲートウェイ欄を空にする。必要なら正しいアドレスに直す",
                        f"If nothing beyond this LAN is needed, clear the gateway field of "
                        f"{iface}; otherwise correct the address",
                    ),
                    tr(f"経路を確認: {show_route}", f"Confirm the route: {show_route}"),
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
                tr(f"経路を確認: {show_route}", f"Confirm the route: {show_route}"),
            ],
        )


__all__: List[str] = ["GatewayProbe", "default_gateway"]
