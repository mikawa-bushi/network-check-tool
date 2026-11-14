"""L2/L3 base: is there a working network interface with an IPv4 address?

This is the bottom of the dependency graph. If no interface is up with an IPv4
address, every higher-layer failure (gateway, DNS, devices) is a consequence,
not a cause, and the engine folds them away.
"""

from __future__ import annotations

from typing import List, Optional

from ..core import CheckResult, Context, Probe, register
from ..i18n import Text, tr
from ..netinfo import KIND_LOOPBACK, KIND_VIRTUAL, Interface, source_ip_for
from ._common import oscmd

_RFC5737_OFF_LAN = "203.0.113.1"


def _primary_ip() -> Optional[str]:
    """Best-effort local IP used for the default route, without sending traffic."""
    return source_ip_for(_RFC5737_OFF_LAN)


def _apipa_only(i: Interface) -> bool:
    return not i.usable_ipv4 and any(a.is_link_local for a in i.ipv4)


def speed_text(mbps: float) -> str:
    return f"{mbps / 1000:g} Gbps" if mbps >= 1000 else f"{mbps:g} Mbps"


def describe_interface(i: Interface) -> str:
    """``イーサネット 192.168.3.10/24 (1 Gbps 全二重)`` style label."""
    addrs = ",".join(a.cidr for a in i.usable_ipv4) or tr("IPなし", "no IPv4")
    extra: List[str] = []
    if i.speed_mbps:
        extra.append(speed_text(i.speed_mbps))
    if i.duplex:
        extra.append(
            tr("全二重", "full-duplex")
            if i.duplex == "full"
            else tr("半二重", "half-duplex")
        )
    return f"{i.name} {addrs}" + (f" ({' '.join(extra)})" if extra else "")


@register
class InterfaceProbe(Probe):
    name = "interface"
    title = Text("ネットワークアダプタ", "Network interface")
    layer = "L2"
    depends_on: List[str] = []
    what = Text(
        "LANケーブル/Wi-Fiがつながり、IPv4アドレスを持つアダプタがあるか確認します。",
        "Checks that a non-loopback interface is UP and holds an IPv4 address.",
    )
    why = Text(
        "リンクとIPアドレスがなければ一切通信できません。これが失敗している間の上位の失敗はすべて結果にすぎません。",
        "Without a link-up interface and an assigned address there is no way to "
        "send any packet; every higher-layer failure below this is a symptom.",
    )
    manual_commands = [
        Text("ipconfig /all (Windows)", "ipconfig /all (Windows)"),
        Text("ip -br addr (Linux)", "ip -br addr (Linux)"),
    ]

    def check(self, ctx: Context) -> CheckResult:
        hn = ctx.network()
        ifaces = [i for i in hn.interfaces if i.kind != KIND_LOOPBACK]
        detail = {
            "interfaces": [i.to_dict() for i in hn.interfaces],
            "primary_ip": _primary_ip(),
            "source": hn.source,
        }
        if not hn.interfaces:
            # Enumeration unavailable: fall back to the routing-socket trick.
            ip = detail["primary_ip"]
            if ip:
                return CheckResult.ok(
                    self.name,
                    tr(
                        f"ローカルIPv4 {ip}（アダプタ一覧は取得できませんでした）",
                        f"local IPv4 {ip} (interface enumeration unavailable on this platform)",
                    ),
                    metrics={"has_ip": 1.0},
                    detail=detail,
                    code="interface.ok_minimal",
                )
            return CheckResult.fail(
                self.name,
                tr(
                    "使用可能なIPv4アドレスが見つかりません",
                    "no usable local IPv4 address found",
                ),
                metrics={"has_ip": 0.0},
                detail=detail,
                code="interface.no_ipv4",
            )

        usable = hn.lan_interfaces()
        physical = [i for i in ifaces if i.kind != KIND_VIRTUAL]
        working = [i for i in usable if i.kind != KIND_VIRTUAL]
        # A virtual adapter (VM host-only, WSL, VPN) keeps its address with
        # every cable unplugged, so it only counts on a PC without physical
        # adapters, or when a linked physical adapter without an IPv4 address
        # may be its bridge or virtual-switch port.
        if not physical or any(i.up and not i.ipv4 for i in physical):
            working += [i for i in usable if i.kind == KIND_VIRTUAL]
        apipa = [i for i in physical or ifaces if i.up and _apipa_only(i)]
        if working:
            names = ", ".join(describe_interface(i) for i in working)
            metrics = {"has_ip": 1.0, "interface_count": float(len(working))}
            if apipa:
                stuck = ", ".join(i.name for i in apipa)
                return CheckResult.warn(
                    self.name,
                    tr(
                        f"{names} は使用可能ですが、{stuck} は 169.254.x.x（自動割当）のみ — DHCPサーバーが無いネットワークです。固定IPの設定が必要です",
                        f"{names} works, but {stuck} only has a 169.254.x.x (APIPA) address — no DHCP server answered; set a static IP",
                    ),
                    metrics=metrics,
                    detail=detail,
                    code="interface.apipa",
                )
            return CheckResult.ok(
                self.name, names, metrics=metrics, detail=detail, code="interface.ok"
            )

        ifaces = physical or ifaces
        up = [i for i in ifaces if i.up]
        dup = [i for i in ifaces if any("duplicate" in a.flags for a in i.ipv4)]
        if dup:
            names = ", ".join(
                f"{i.name} {a.ip}"
                for i in dup
                for a in i.ipv4
                if "duplicate" in a.flags
            )
            return CheckResult.fail(
                self.name,
                tr(
                    f"IPアドレスが他の機器と重複しているため使用できません: {names}",
                    f"IP address unusable because it conflicts with another device: {names}",
                ),
                metrics={"has_ip": 0.0},
                detail=detail,
                code="interface.duplicate_ip",
            )
        if apipa:
            names = ", ".join(i.name for i in apipa)
            return CheckResult.fail(
                self.name,
                tr(
                    f"{names} は 169.254.x.x（自動割当）のみ — DHCPサーバーが無いネットワークです。固定IPの設定が必要です",
                    f"{names} only has a 169.254.x.x (APIPA) address — no DHCP server answered; set a static IP",
                ),
                metrics={"has_ip": 0.0},
                detail=detail,
                code="interface.apipa",
            )
        if up:
            names = ", ".join(i.name for i in up)
            return CheckResult.fail(
                self.name,
                tr(
                    f"{names} はリンクしていますがIPv4アドレスがありません",
                    f"interface(s) {names} are UP but have no IPv4 address "
                    f"(DHCP failure or unplugged after link-up?)",
                ),
                metrics={"has_ip": 0.0},
                detail=detail,
                code="interface.no_ipv4",
            )
        summary = tr(
            "リンクしているアダプタがありません（LANケーブル未接続 / アダプタ無効 / Wi-Fi未接続）",
            "no interface is UP with an IPv4 address (cable unplugged, adapter disabled or Wi-Fi not connected)",
        )
        if usable:
            virtual = ", ".join(i.name for i in usable)
            summary += tr(
                f"。{virtual} は仮想アダプタのため数えません",
                f"; {virtual} is virtual and does not count",
            )
        return CheckResult.fail(
            self.name,
            summary,
            metrics={"has_ip": 0.0},
            detail=detail,
            code="interface.no_link",
        )

    def advice(self, result, results=None):
        code = result.code if result is not None else ""
        if code == "interface.apipa":
            return (
                tr(
                    "DHCPでIPアドレスを取得できていません。設備ネットワークは固定IPが一般的です。",
                    "The PC could not get an address from DHCP; equipment networks usually use static IPs.",
                ),
                [
                    tr(
                        "機器と同じネットワークの固定IPを設定する（例: 機器が 192.168.3.39/24 なら PC は 192.168.3.100/24 など未使用の番号）",
                        "Assign a static IP in the equipment's subnet (e.g. device 192.168.3.39/24 -> PC 192.168.3.100/24, an unused number)",
                    ),
                    tr(
                        "Windows: 設定 → ネットワークとインターネット → アダプターのオプション → IPv4 のプロパティ",
                        "Windows: Settings → Network → Adapter options → IPv4 properties",
                    ),
                    oscmd(
                        "nmcli con mod "
                        + tr("<接続名>", "<connection>")
                        + " ipv4.method manual ipv4.addresses 192.168.3.100/24",
                        'netsh interface ip set address "'
                        + tr("イーサネット", "Ethernet")
                        + '" static 192.168.3.100 255.255.255.0',
                    ),
                ],
            )
        if code == "interface.duplicate_ip":
            return (
                tr(
                    "このPCのIPアドレスを別の機器がすでに使っています（IP重複）。",
                    "Another device on the LAN already uses this PC's IP address (IP conflict).",
                ),
                [
                    tr(
                        "このPCに別の未使用IPを設定する",
                        "Give this PC a different, unused IP",
                    ),
                    tr(
                        "IP管理表で重複相手を確認する（netdiag scan で一覧化できます）",
                        "Find the other device in the IP plan (netdiag scan lists the subnet)",
                    ),
                ],
            )
        return (
            tr(
                "使用できるネットワークアダプタがありません（リンクが無い、またはIP未設定）。",
                "No usable network interface — the link is down or no IP was assigned.",
            ),
            [
                tr(
                    "LANケーブルの抜け・ハブ/スイッチのリンクランプ、Wi-Fiの接続を確認する",
                    "Check the cable / switch link LED / Wi-Fi association",
                ),
                tr(
                    "アダプタが無効になっていないか確認: "
                    + oscmd("ip link", "ncpa.cpl", "ifconfig"),
                    "Make sure the adapter is enabled: "
                    + oscmd("ip link", "ncpa.cpl", "ifconfig"),
                ),
                oscmd("ip -br addr", "ipconfig /all", "ifconfig"),
            ],
        )
