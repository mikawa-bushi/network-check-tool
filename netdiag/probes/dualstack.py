"""L3: compare IPv4 vs IPv6 reachability to the same destination.

Single-stack failures (IPv6 broken while IPv4 works, or vice versa) are common
and confusing because some apps prefer one family. Measuring both at once makes
the asymmetry obvious.
"""

from __future__ import annotations

import errno
import socket
from typing import Optional

from ..core import CheckResult, Context, Probe, register
from ..i18n import Text, tr
from ..measure import system_ping


def _ping_family(host: str, v6: bool, count: int = 2) -> Optional[bool]:
    """Ping ``host`` over one IP family. True=reachable, False=no reply, None=untested."""
    st = system_ping(host, count=count, family=6 if v6 else 4)
    if st.error or (not st.raw and not st.received):
        return None  # ping could not run for this family
    return st.received > 0


def _tcp_family(
    host: str, family: int, port: int = 443, timeout: float = 3.0
) -> Optional[bool]:
    """TCP connect over one family — what applications actually do.

    Second opinion when ping fails: many sites drop ICMP. ``None`` when the
    family has no address or no route on this PC.
    """
    try:
        infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return None
    addr = infos[0][4]
    try:
        with socket.create_connection(addr[:2], timeout=timeout):
            return True
    except OSError as e:
        if getattr(e, "errno", None) in (errno.ENETUNREACH, errno.EADDRNOTAVAIL):
            return None  # no route for this family on this PC
        return False


def _family_ok(host: str, v6: bool, count: int = 2) -> Optional[bool]:
    """True/False, or None when this PC does not use the family (single stack).

    Without a reply to ping, TCP decides. ping counts "Network is unreachable"
    as lost replies, so only TCP's None says the PC has no route or address
    for the family.
    """
    if _ping_family(host, v6=v6, count=count):
        return True
    return _tcp_family(host, socket.AF_INET6 if v6 else socket.AF_INET)


def _has_family(host: str, family: int) -> bool:
    try:
        socket.getaddrinfo(host, None, family)
        return True
    except (socket.gaierror, UnicodeError):
        return False


@register
class DualStackProbe(Probe):
    name = "dualstack"
    title = Text("IPv4/IPv6 の整合", "IPv4/IPv6 parity")
    layer = "L3"
    depends_on = ["dns"]
    requires_internet = True
    what = Text(
        "同じホスト名に IPv4 と IPv6 の両方で接続（ping、だめならTCP 443）して比較します。",
        "Reaches the same hostname over IPv4 and IPv6 (ping, else TCP 443) and compares.",
    )
    why = Text(
        "片方のアドレスファミリだけ壊れていると、アプリによって症状が出たり出なかったりします。",
        "One broken address family while the other works produces app-specific, "
        "intermittent failures. Comparing both surfaces the asymmetry directly.",
    )
    manual_commands = ["ping -4 <host>", "ping -6 <host>"]

    def check(self, ctx: Context) -> CheckResult:
        host = ctx.get("dns_name", "example.com")
        # Two pings per family keep this cheap; --count is for the gateway and
        # the devices, and would cost a second per ping with ping.exe here.
        has4 = _has_family(host, socket.AF_INET)
        has6 = _has_family(host, socket.AF_INET6)
        v4 = _family_ok(host, v6=False) if has4 else None
        v6 = _family_ok(host, v6=True) if has6 else None
        detail = {
            "host": host,
            "ipv4_ok": v4,
            "ipv6_ok": v6,
            "has_a": has4,
            "has_aaaa": has6,
        }
        metrics = {}
        if v4 is not None:
            metrics["ipv4_ok"] = 1.0 if v4 else 0.0
        if v6 is not None:
            metrics["ipv6_ok"] = 1.0 if v6 else 0.0

        if v4 and v6:
            return CheckResult.ok(
                self.name,
                tr(
                    f"{host}: IPv4/IPv6 とも到達可能",
                    f"{host}: IPv4 and IPv6 both reachable",
                ),
                metrics=metrics,
                detail=detail,
                code="dualstack.ok",
            )
        if v4 and v6 is False:
            return CheckResult.warn(
                self.name,
                tr(
                    f"{host}: IPv4 はOK、IPv6 は到達不可（IPv6優先のアプリが待たされます）",
                    f"{host}: IPv4 OK but IPv6 unreachable (apps preferring v6 will stall)",
                ),
                metrics=metrics,
                detail=detail,
                code="dualstack.v6_broken",
            )
        if v6 and v4 is False:
            return CheckResult.warn(
                self.name,
                tr(
                    f"{host}: IPv6 はOK、IPv4 は到達不可",
                    f"{host}: IPv6 OK but IPv4 unreachable",
                ),
                metrics=metrics,
                detail=detail,
                code="dualstack.v4_broken",
            )
        if (v4 and v6 is None) or (v6 and v4 is None):
            works, other = ("IPv4", "IPv6") if v4 else ("IPv6", "IPv4")
            return CheckResult.ok(
                self.name,
                tr(
                    f"{host}: {works} は到達可能（このPCでは {other} は未使用）",
                    f"{host}: {works} reachable ({other} not in use on this PC)",
                ),
                metrics=metrics,
                detail=detail,
                code="dualstack.single_stack",
            )
        if v4 is None and v6 is None:
            return CheckResult.skip(
                self.name,
                tr(
                    "pingできないかアドレスがありません",
                    "no ping or no addresses to test",
                ),
                code="dualstack.untested",
            )
        return CheckResult.fail(
            self.name,
            tr(
                f"{host}: IPv4/IPv6 とも到達不可",
                f"{host}: neither IPv4 nor IPv6 reachable",
            ),
            metrics=metrics,
            detail=detail,
            code="dualstack.unreachable",
        )

    def advice(self, result, results=None):
        if result is not None and result.code == "dualstack.unreachable":
            host = result.detail.get("host", "")
            return (
                tr(
                    f"{host} に IPv4/IPv6 のどちらでも接続できません（ping・TCP 443 とも）。",
                    f"{host} is unreachable over both IPv4 and IPv6 (ping and TCP 443).",
                ),
                [
                    tr(
                        "比較用のホスト名（--dns-name）が外部から到達可能か確認する",
                        "Check that the comparison host (--dns-name) is reachable at all",
                    ),
                    tr(
                        "プロキシ経由でしか外に出られない社内ネットワークでは、この確認は当てはまりません",
                        "On networks that only allow traffic through a proxy this check does not apply",
                    ),
                ],
            )
        return (
            tr(
                "IPv4 か IPv6 の片方だけが壊れています。",
                "One IP family (v4 or v6) is broken while the other works.",
            ),
            [
                tr(
                    "IPv6 が壊れている場合はルーターのRA設定を確認するか、一時的にIPv6を無効化して確認する",
                    "If IPv6 is broken, check router advertisements / disable v6 to confirm.",
                ),
                tr(
                    "ping -4 と ping -6 で同じホストを比較する",
                    "Compare `ping -4` and `ping -6` to the same host.",
                ),
            ],
        )
