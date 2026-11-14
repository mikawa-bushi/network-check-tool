"""ICMP reachability (precise, unprivileged — see :mod:`netdiag.measure.icmp`).

``external`` pings a fixed off-LAN IP (no DNS involved) when the network is
expected to reach the internet; ``icmp`` is the ad-hoc single-target ping.
"""

from __future__ import annotations

from typing import Optional, Tuple

from ..core import CheckResult, Context, Probe, register
from ..i18n import Text, tr
from ..measure import PingStats
from ..measure import ping as _ping
from ._common import classify_latency, ping_settings, ping_summary


def ping(
    host: str, count: int = 4, timeout: float = 2.0, family: Optional[int] = None
) -> Tuple[Optional[float], float, str]:
    """Compatibility wrapper: ``(avg_rtt_ms or None, loss_pct, raw_output)``."""
    st = _ping(host, count=count, timeout=timeout, family=family)
    avg = st.rtt.avg if st.rtt else None
    return avg, st.loss_pct, st.raw or ping_summary(st)


def result_from_ping(
    name: str,
    host: str,
    st: PingStats,
    on_link: bool = False,
    unreachable_hint: str = "",
) -> CheckResult:
    """Shared OK/WARN/FAIL classification for single-host ping probes."""
    metrics = st.to_metrics()
    detail = {"host": host, "ping": st.to_dict()}
    if st.error:
        return CheckResult.unknown(
            name,
            tr(
                f"{host}: 測定できません（{st.error}）",
                f"{host}: cannot measure ({st.error})",
            ),
            detail=detail,
            code=f"{name}.unmeasured",
        )
    if not st.any_reply:
        return CheckResult.fail(
            name,
            tr(
                f"{host} が応答しません（{ping_summary(st)}）{unreachable_hint}",
                f"{host} unreachable ({ping_summary(st)}){unreachable_hint}",
            ),
            metrics=metrics,
            detail=detail,
            code=f"{name}.unreachable",
        )
    slow = classify_latency(st, on_link=on_link)
    if st.lost or slow:
        reason = slow or tr("パケットロスあり", "packet loss")
        return CheckResult.warn(
            name,
            f"{host}: {reason} ({ping_summary(st)})",
            metrics=metrics,
            detail=detail,
            code=f"{name}.degraded",
        )
    return CheckResult.ok(
        name,
        f"{host} ({ping_summary(st)})",
        metrics=metrics,
        detail=detail,
        code=f"{name}.ok",
    )


@register
class ExternalProbe(Probe):
    """Reachability of a stable off-LAN IP address.

    Defaults to an IP (not a hostname) so this stays independent of DNS — that
    separation is exactly what lets the engine distinguish a routing/WAN outage
    from a name-resolution problem. In a closed network, point ``external_ip`` at
    an in-site address that should always answer.
    """

    name = "external"
    title = Text("外部への到達性", "External reachability")
    layer = "L3"
    depends_on = ["gateway"]
    requires_internet = True
    what = Text(
        "LAN外の固定IPアドレスにpingします（DNSを使いません）。",
        "Pings a fixed off-LAN IP address (no DNS involved).",
    )
    why = Text(
        "『パケットがLANの外へ出て戻れるか』と『名前解決できるか』を分けます。これがOKでDNSがNGなら原因はDNSです。",
        "Separates 'can packets leave the LAN and come back' from 'can we resolve "
        "names'. If this works but DNS fails, the fault is DNS, not the WAN.",
    )
    manual_commands = ["ping 1.1.1.1"]

    def check(self, ctx: Context) -> CheckResult:
        host = ctx.get("external_ip", "1.1.1.1")
        st = _ping(host, **ping_settings(ctx.get))
        return result_from_ping(
            self.name,
            host,
            st,
            unreachable_hint=tr(
                " — WAN/上位回線または経路の問題", " — WAN/upstream or routing problem"
            ),
        )

    def advice(self, result, results=None):
        return (
            tr(
                "LANは正常ですが外部に出られません — 上位回線/ルーター設定/経路の問題です。",
                "The LAN works but traffic can't reach the outside — a WAN/upstream fault.",
            ),
            [
                tr(
                    "モデム/ONU・ルーターのランプを確認する",
                    "Check the modem/ONU status lights.",
                ),
                tr(
                    "経路のどこで止まるか確認: netdiag check --only traceroute --internet",
                    "See the last responding hop: netdiag check --only traceroute --internet",
                ),
                tr(
                    "社内ネットワークならプロキシ経由が必要か、ネットワーク管理者に確認する",
                    "On a corporate network, ask whether traffic must go through a proxy.",
                ),
            ],
        )


@register
class PingProbe(Probe):
    """Ad-hoc ping of a user-supplied target (``ctx["target"]``)."""

    name = "icmp"
    title = Text("ICMP ping", "ICMP ping")
    layer = "L3"
    depends_on = ["gateway"]
    default = False  # needs a target; see `netdiag ping` / device checks
    what = Text("指定した相手にpingします。", "Pings an arbitrary target host.")
    why = Text(
        "指定した相手への到達性・遅延・パケットロスを測ります。",
        "Measures raw reachability, RTT and packet loss to a chosen destination.",
    )
    manual_commands = ["ping -c 10 <host>"]

    def check(self, ctx: Context) -> CheckResult:
        host = ctx.get("target")
        if not host:
            return CheckResult.skip(
                self.name,
                tr("対象が指定されていません", "no target specified"),
                code="icmp.no_target",
            )
        return result_from_ping(self.name, host, _ping(host, **ping_settings(ctx.get)))
