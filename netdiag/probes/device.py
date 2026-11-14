"""Per-device probes: can this PC talk to one PLC / HMI / robot controller?

Three target-scoped probes run once per device, bottom-up, so the engine names
the lowest broken step as the cause:

* ``route`` (L3 config) — is the device in one of this PC's subnets, or behind
  a gateway? The #1 field mistake is a laptop in 192.168.1.x trying to reach a
  PLC in 192.168.3.x: no cable or device fault, just no route.
* ``reach`` (L2/L3) — precise ping statistics plus ARP evidence. ARP tells a
  powered-off/miswired device (no ARP answer) apart from a live device that
  merely ignores ping, and duplicate replies or an unexpected MAC reveal IP
  conflicts.
* ``ports`` (L4) — TCP connect to the device's service ports (Modbus/TCP 502,
  EtherNet/IP 44818, OPC UA 4840 ...): open, closed (alive, nothing listening)
  or no answer.

The device is ``ctx.config["target"]`` (a dict, see :mod:`netdiag.inventory`).
The device runner measures every device in one fast ping sweep and hands each
one its :class:`DeviceEvidence` as ``ctx.config["evidence"]``; without it, the
probes measure on their own.
"""

from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..core import CheckResult, Context, Probe, register
from ..i18n import Text, tr
from ..inventory import SERVICE_NAMES
from ..measure import PingStats, ping, tcp_check_many
from ..measure.icmp import _literal_family, _resolve_v4
from ..netinfo import model as nm
from ._common import classify_latency, fmt_ms, oscmd, ping_settings, ping_summary


@dataclass(frozen=True)
class DeviceEvidence:
    """What the device runner measured for one device before its probes ran.

    ``address`` is None when the host did not resolve. ``ping`` is None when
    the device was not pinged because its route already fails.
    """

    address: Optional[str]
    ping: Optional[PingStats] = None
    ping_window: float = 0.0
    neighbor_before: Optional[nm.Neighbor] = None
    neighbor_after: Optional[nm.Neighbor] = None


def evidence(ctx: Context) -> Optional[DeviceEvidence]:
    return ctx.get("evidence")


def target(ctx: Context) -> Dict[str, Any]:
    t = ctx.get("target") or {}
    if isinstance(t, str):
        t = {"name": t, "host": t}
    return t


def port_label(port: int) -> str:
    svc = SERVICE_NAMES.get(port)
    return f"{port}/{svc}" if svc else str(port)


def l2_evidence(
    before: Optional[nm.Neighbor], after: Optional[nm.Neighbor], window_s: float
) -> str:
    """``present`` / ``absent`` / ``unknown`` from ARP state around a ping run.

    Only *fresh* evidence counts: an entry confirmed during the test (Linux
    reports the confirmation age), or one that appeared during it. A cache
    entry left over from before proves nothing — the device may have died
    seconds ago. Linux still lists such an entry as REACHABLE for up to 45 s.
    """
    if after is None:
        return "unknown"
    if after.absent:
        return "absent"
    if not after.mac or nm.mac_is_null(after.mac) or after.state == nm.NEIGH_PERMANENT:
        return "unknown"
    if after.confirmed_age is not None:
        fresh = (
            after.state == nm.NEIGH_REACHABLE and after.confirmed_age <= window_s + 1.0
        )
        return "present" if fresh else "unknown"
    if before is None or not before.mac:
        return "present"  # resolved during our test
    if after.state == nm.NEIGH_REACHABLE and before.state != nm.NEIGH_REACHABLE:
        return "present"
    return "unknown"


def _default_route_only(hn: nm.HostNetwork, ip: str) -> bool:
    """No route but the default one covers *ip*."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not any(
        r.network is not None and r.network.prefixlen > 0 and addr in r.network
        for r in hn.routes
    )


@register
class RouteProbe(Probe):
    name = "route"
    title = Text("経路（サブネット）", "Route / subnet")
    layer = "L3"
    scope = "target"
    depends_on: List[str] = []
    what = Text(
        "機器のIPがこのPCのどのネットワークに属するか（直接つながるか、ゲートウェイ経由か）を判定します。",
        "Decides whether the device is in one of this PC's subnets or behind a gateway.",
    )
    why = Text(
        "PCと機器のサブネットが違うと、ケーブルも機器も正常なのに通信できません。現場で最も多い原因です。",
        "If the PC and the device are in different subnets nothing gets through, "
        "even with a perfect cable and a healthy device — the most common field mistake.",
    )
    manual_commands = ["ipconfig", "route print -4", "ip route get <device-ip>"]

    def check(self, ctx: Context) -> CheckResult:
        t = target(ctx)
        host = str(t.get("host") or "").strip()
        if not host:
            return CheckResult.skip(
                self.name,
                tr("機器のアドレスが未指定", "no device address given"),
                code="route.no_host",
            )
        ev = evidence(ctx)
        if ev is not None:
            ip = ev.address
        elif _literal_family(host) is None:
            ip = _resolve_v4(host, timeout=float(ctx.get("dns_timeout", 3.0)))
        else:
            ip = host
        if ip is None:
            return CheckResult.fail(
                self.name,
                tr(
                    "ホスト名を解決できません（DNS/hostsファイルを確認、またはIPで指定）",
                    "cannot resolve the host name (check DNS/hosts, or use the IP)",
                ),
                detail={"host": host},
                code="route.dns",
            )
        hn = ctx.network()
        dec = hn.decide_route(ip)
        detail = {
            "host": host,
            "address": ip,
            "on_link": dec.on_link,
            "arp": dec.arp,
            "interface": dec.interface.name if dec.interface else None,
            "local_address": dec.address.cidr if dec.address else None,
            "gateway": dec.gateway,
            "special": dec.special,
            "candidates": [f"{i.name} {a.cidr}" for i, a in dec.candidates],
        }
        if dec.special == "ipv6":
            detail["on_link"] = None
            return CheckResult.ok(
                self.name,
                tr(
                    "IPv6アドレス（経路判定は省略）",
                    "IPv6 address (route check skipped)",
                ),
                detail=detail,
                code="route.ipv6",
            )
        if dec.special == "invalid":
            return CheckResult.fail(
                self.name,
                tr("IPアドレスの形式が不正", "invalid IP address"),
                detail=detail,
                code="route.invalid",
            )
        if dec.special == "self":
            return CheckResult.warn(
                self.name,
                tr(
                    "このPC自身のIPアドレスです（機器のIPと重複していませんか?）",
                    "this is the PC's own address (does it clash with the device?)",
                ),
                detail=detail,
                code="route.self",
            )
        if dec.special in ("network", "broadcast"):
            net = dec.address.network if dec.address else ""
            return CheckResult.fail(
                self.name,
                tr(
                    f"{net} のネットワーク/ブロードキャストアドレスです（機器には使えない番号）",
                    f"network/broadcast address of {net} (not usable for a device)",
                ),
                detail=detail,
                code="route.special_address",
            )
        if dec.special == "loopback":
            return CheckResult.ok(
                self.name,
                tr("このPC内部", "local loopback"),
                detail=detail,
                code="route.loopback",
            )

        if dec.on_link and dec.interface is not None:
            iface, addr = dec.interface, dec.address
            if not iface.up:
                return CheckResult.fail(
                    self.name,
                    tr(
                        f"経路のNIC『{iface.name}』がリンクダウン（ケーブル/スイッチを確認）",
                        f"the NIC '{iface.name}' for this subnet has no link (check cable/switch)",
                    ),
                    detail=detail,
                    code="route.link_down",
                )
            # The OS picks among NICs only for an equally specific subnet on an
            # up link; a longer prefix or a link-down NIC leaves no choice.
            others = [
                i.name
                for i, a in dec.candidates
                if i.name != iface.name
                and i.up
                and addr is not None
                and a.prefixlen == addr.prefixlen
            ]
            via = f"{iface.name} {addr.cidr}" if addr else iface.name
            if others:
                return CheckResult.warn(
                    self.name,
                    tr(
                        f"同じサブネットが複数のNIC（{iface.name}, {', '.join(others)}）にあり、送信NICが不定",
                        f"subnet present on several NICs ({iface.name}, {', '.join(others)}) — outgoing NIC is ambiguous",
                    ),
                    detail=detail,
                    code="route.ambiguous",
                )
            return CheckResult.ok(
                self.name,
                tr(f"同一サブネット: {via} から直接", f"on-link via {via}"),
                detail=detail,
                code="route.on_link",
            )
        local = ", ".join(
            a.cidr for i in hn.lan_interfaces() for a in i.usable_ipv4
        ) or tr("なし", "none")
        if dec.gateway and _default_route_only(hn, ip):
            # The route to the internet, not one to this device. A routed
            # device answers through it and a device in another subnet on this
            # line does not; the device runner settles which (reconcile_device).
            return CheckResult.ok(
                self.name,
                tr(
                    f"別サブネット — ゲートウェイ {dec.gateway} 経由",
                    f"routed via gateway {dec.gateway}",
                ),
                detail={**detail, "local_networks": local},
                code="route.via_default",
            )
        if dec.gateway:
            return CheckResult.ok(
                self.name,
                tr(
                    f"別サブネット — ゲートウェイ {dec.gateway} 経由",
                    f"routed via gateway {dec.gateway}",
                ),
                detail=detail,
                code="route.via_gateway",
            )
        return CheckResult.fail(
            self.name,
            tr(
                f"このPCのネットワーク（{local}）に含まれず、ゲートウェイもありません — サブネット不一致",
                f"not in this PC's subnets ({local}) and no gateway — subnet mismatch",
            ),
            detail={**detail, "local_networks": local},
            code="route.no_route",
        )

    def advice(self, result, results=None):
        code = result.code if result is not None else ""
        d = result.detail if result is not None else {}
        ip = d.get("address") or tr("<機器IP>", "<device-ip>")
        if code in ("route.no_route", "route.default_only"):
            try:
                net24 = ipaddress.ip_network(f"{ip}/24", strict=False)
                example = f"{'.'.join(str(net24.network_address).split('.')[:3])}.x/24"
            except ValueError:
                example = tr("機器と同じネットワーク", "the device's subnet")
            return (
                tr(
                    f"このPCは {ip} と同じネットワークにいません（IP設定の不一致）。",
                    f"This PC is not in the same subnet as {ip} (IP settings mismatch).",
                ),
                [
                    tr(
                        f"PCのIPを {example} の未使用番号に設定する（サブネットマスクは機器と同じに。多くは 255.255.255.0）",
                        f"Give the PC an unused address in {example} (same mask as the device, usually 255.255.255.0)",
                    ),
                    tr(
                        "今の設定を残したい場合は『IPv4の詳細設定』でIPアドレスを追加できます（Windows）",
                        "Windows can keep the current IP and add a second one under IPv4 'Advanced' settings",
                    ),
                    oscmd(
                        f"sudo ip addr add {example.replace('.x/24', '.200/24')} dev <iface>",
                        f'netsh interface ip add address "{tr("イーサネット", "Ethernet")}" '
                        f"{example.replace('.x/24', '.200')} 255.255.255.0",
                    ),
                    tr(
                        "機器側のIP・サブネットマスク設定が想定どおりかも確認する",
                        "Also confirm the device's own IP/mask settings",
                    ),
                ]
                + (
                    [
                        tr(
                            f"機器がルーターの先にある構成なら、ゲートウェイ {d.get('gateway')} から先の経路を確認する: "
                            + oscmd(f"tracepath {ip}", f"tracert -d {ip}"),
                            f"If the device really sits behind a router, check the path past {d.get('gateway')}: "
                            + oscmd(f"tracepath {ip}", f"tracert -d {ip}"),
                        )
                    ]
                    if code == "route.default_only"
                    else []
                ),
            )
        if code == "route.link_down":
            return (
                tr(
                    "機器へ向かうNICがリンクしていません。",
                    "The NIC towards the device has no link.",
                ),
                [
                    tr(
                        "LANケーブルの挿し直し・交換、スイッチのリンクランプを確認",
                        "Reseat/replace the cable; check the switch link LED",
                    ),
                    tr(
                        "NICが無効になっていないか確認",
                        "Make sure the adapter is enabled",
                    ),
                ],
            )
        if code == "route.dns":
            return (
                tr(
                    "機器名をIPアドレスに変換できません。",
                    "The device name cannot be resolved.",
                ),
                [
                    tr(
                        "台帳にはIPアドレスで書くのが確実です",
                        "Use the IP address in the device list",
                    )
                ],
            )
        return (
            tr(
                "機器のアドレス指定または経路に問題があります。",
                "The device address or route is wrong.",
            ),
            [
                oscmd(f"ip route get {ip}", "route print -4"),
                tr(
                    "台帳のIPアドレスを確認する", "Check the address in the device list"
                ),
            ],
        )


@register
class ReachProbe(Probe):
    name = "reach"
    title = Text("応答（ping/ARP）", "Reachability (ping/ARP)")
    layer = "L3"
    scope = "target"
    depends_on = ["route"]
    what = Text(
        "機器にpingを複数回送り、応答率・遅延（中央値/p95/最大）・ジッタを測ります。同一サブネットならARPで機器の存在も確認します。",
        "Pings the device repeatedly (loss, median/p95/max latency, jitter) and, on-link, checks ARP.",
    )
    why = Text(
        "『機器が止まっている/配線不良』『機器は生きているがpingを無視』『IP重複』『断続的なロス』を区別します。",
        "Separates a dead or miswired device, a live device that ignores ping, an IP conflict and intermittent loss.",
    )
    manual_commands = [
        "ping -n 20 <device-ip>",
        "arp -a <device-ip>",
        "ping -c 20 <device-ip> ; ip neigh show <device-ip>",
    ]

    def check(self, ctx: Context) -> CheckResult:
        t = target(ctx)
        route = ctx.result("route")
        rd = route.detail if route is not None else {}
        ip = rd.get("address") or t.get("host")
        on_link = bool(rd.get("on_link"))
        # ARP proves presence or absence only on an ARP link of this PC; a
        # tunnel or point-to-point route is on-link but has no ARP at all.
        arp = bool(rd.get("arp"))

        ev = evidence(ctx)
        st: Optional[PingStats]
        if ev is not None:
            st, before, after = ev.ping, ev.neighbor_before, ev.neighbor_after
            window = max(ev.ping_window, 1.0)
        else:
            from .. import netinfo

            before = netinfo.neighbor(ip) if arp else None
            started = time.monotonic()
            st = ping(ip, **ping_settings(ctx.get))
            window = max(time.monotonic() - started, 1.0)
            after = netinfo.neighbor(ip) if arp else None
        if st is None:
            return CheckResult.unknown(
                self.name,
                tr("pingで測定していません", "not measured by ping"),
                code="reach.unmeasured",
            )
        l2 = l2_evidence(before, after, window) if arp else "n/a"
        mac = after.mac if after is not None and after.mac else None

        from .. import oui

        vendor = oui.lookup(mac)
        detail: Dict[str, Any] = {
            "address": ip,
            "on_link": on_link,
            "arp": arp,
            "ping": st.to_dict(),
            "mac": mac,
            "vendor": vendor,
            "arp_state": after.state if after is not None else None,
            "l2": l2,
        }
        metrics = st.to_metrics()
        mac_txt = f" MAC {mac}" + (f" ({vendor})" if vendor else "") if mac else ""
        expected_mac = nm.normalize_mac(t.get("mac"))
        has_ports = bool(t.get("ports"))

        if st.error:
            return CheckResult.unknown(
                self.name,
                tr(f"測定できません（{st.error}）", f"cannot measure ({st.error})"),
                detail=detail,
                code="reach.unmeasured",
            )
        if st.duplicates:
            return CheckResult.fail(
                self.name,
                tr(
                    f"1回のpingに複数の応答（{st.duplicates}件）— IPアドレス重複またはネットワークのループの疑い",
                    f"duplicate replies ({st.duplicates}) — IP conflict or a network loop",
                ),
                metrics=metrics,
                detail=detail,
                code="reach.duplicate_reply",
            )
        if expected_mac and mac and expected_mac != mac:
            return CheckResult.fail(
                self.name,
                tr(
                    f"台帳のMAC {expected_mac} と異なる機器 {mac}{f' ({vendor})' if vendor else ''} が応答 — 機器交換かIP重複",
                    f"answered by {mac}{f' ({vendor})' if vendor else ''}, not the registered {expected_mac} — replaced device or IP conflict",
                ),
                metrics=metrics,
                detail=detail,
                code="reach.mac_mismatch",
            )

        if not st.any_reply:
            if l2 == "present":
                return CheckResult.warn(
                    self.name,
                    tr(
                        f"pingに応答しませんが、ARPには応答（機器は存在）—{mac_txt} ICMP無効/フィルタの可能性",
                        f"no ping reply, but it answers ARP (device is present) —{mac_txt} ICMP disabled/filtered?",
                    ),
                    metrics=metrics,
                    detail=detail,
                    code="reach.icmp_blocked",
                )
            if l2 == "absent":
                return CheckResult.fail(
                    self.name,
                    tr(
                        f"ping・ARPとも応答なし（{ping_summary(st)}）— 電源・ケーブル・機器のIP設定を確認",
                        f"no reply ({ping_summary(st)}) and no ARP answer — check power, cabling and the device's IP",
                    ),
                    metrics=metrics,
                    detail=detail,
                    code="reach.no_arp",
                )
            if has_ports:
                # ICMP is commonly blocked on routed paths; let the port check decide.
                return CheckResult.warn(
                    self.name,
                    tr(
                        "pingに応答なし（ポート確認で判定します）",
                        "no ping reply (deferring to the port check)",
                    ),
                    metrics=metrics,
                    detail=detail,
                    code="reach.no_icmp",
                )
            return CheckResult.fail(
                self.name,
                tr(
                    f"pingに応答なし（{ping_summary(st)}）",
                    f"no reply ({ping_summary(st)})",
                ),
                metrics=metrics,
                detail=detail,
                code="reach.no_reply",
            )

        problems: List[str] = []
        loss_limit = t.get("loss_warn_pct")
        if st.lost and (loss_limit is None or st.loss_pct > float(loss_limit)):
            problems.append(
                tr(
                    "パケットロスあり（断続的な通信断）",
                    "packet loss (intermittent drops)",
                )
            )
        rtt_limit = t.get("rtt_warn_ms")
        r = st.rtt
        if rtt_limit is not None and r is not None and r.median > float(rtt_limit):
            problems.append(
                tr(
                    f"遅延が基準 {fmt_ms(float(rtt_limit))} を超過",
                    f"latency above the {fmt_ms(float(rtt_limit))} limit",
                )
            )
        elif rtt_limit is None:
            slow = classify_latency(st, on_link=on_link)
            if slow:
                problems.append(slow)
        if problems:
            return CheckResult.warn(
                self.name,
                f"{'; '.join(problems)} — {ping_summary(st)}{mac_txt}",
                metrics=metrics,
                detail=detail,
                code="reach.loss" if st.lost else "reach.latency",
            )
        return CheckResult.ok(
            self.name,
            f"{ping_summary(st)}{mac_txt}",
            metrics=metrics,
            detail=detail,
            code="reach.ok",
        )

    def advice(self, result, results=None):
        code = result.code if result is not None else ""
        d = result.detail if result is not None else {}
        ip = d.get("address") or tr("<機器IP>", "<device-ip>")
        if code == "reach.no_arp":
            return (
                tr(
                    f"{ip} の機器がネットワーク上に見つかりません（ARP応答なし）。",
                    f"No device answers for {ip} on the LAN (no ARP reply).",
                ),
                [
                    tr(
                        "機器の電源とLANポートのリンクランプ、スイッチ側のランプを確認する",
                        "Check the device power and the link LEDs on the device and switch",
                    ),
                    tr(
                        "LANケーブル・コネクタを挿し直す／交換する",
                        "Reseat or replace the cable and connectors",
                    ),
                    tr(
                        f"機器側のIP設定が本当に {ip} か確認する（機器のパネル/設定ツール）",
                        f"Confirm the device is really configured as {ip} (front panel / config tool)",
                    ),
                    tr(
                        "同じスイッチにつながる他の機器に届くか確認し、スイッチ/VLANの問題を切り分ける",
                        "Check other devices on the same switch to rule out the switch/VLAN",
                    ),
                ],
            )
        if code in ("reach.duplicate_reply", "reach.mac_mismatch"):
            return (
                tr(
                    f"{ip} を複数の機器が使っている（IP重複）か、別の機器に置き換わっています。",
                    f"Several devices claim {ip} (IP conflict) or the device was replaced.",
                ),
                [
                    tr(
                        f"ARP表で応答したMACを確認し、どの機器か特定する: {oscmd(f'ip neigh show {ip}', f'arp -a {ip}')}",
                        f"Identify the answering MAC in the ARP table: {oscmd(f'ip neigh show {ip}', f'arp -a {ip}')}",
                    ),
                    tr(
                        "最近交換した機器・持ち込みPC・保守用ノートPCの固定IPを確認する",
                        "Check recently replaced devices and laptops with static IPs",
                    ),
                    tr(
                        "重複が無ければ台帳のMACを更新する",
                        "If no conflict, update the MAC in the device list",
                    ),
                ],
            )
        if code == "reach.icmp_blocked":
            return (
                tr(
                    "機器は存在しますがpingに応答しない設定です。",
                    "The device is present but does not answer ping.",
                ),
                [
                    tr(
                        "台帳に使用ポート（例: Modbus 502）を書くと、ポート接続で動作確認できます",
                        "List its service ports (e.g. Modbus 502) to verify it via TCP instead",
                    ),
                    tr(
                        "機器の設定でICMP（ping）応答が無効になっていないか確認する",
                        "Check whether ICMP echo is disabled in the device settings",
                    ),
                ],
            )
        if code in ("reach.loss", "reach.latency"):
            return (
                tr(
                    "通信はできていますが、ロスまたは遅延があり不安定です。",
                    "The device answers, but with loss or delay (unstable link).",
                ),
                [
                    tr(
                        "回数を増やして再測定（例: --count 300）し、ロス率の95%上限が十分小さくなるまで測る",
                        "Re-measure longer (e.g. --count 300) until the 95% loss bound is small enough",
                    ),
                    tr(
                        "同じスイッチの他の機器にもロスがあるか比較する（全体→スイッチ/上流、1台だけ→その機器のケーブル/ポート）",
                        "Compare with other devices on the same switch (all → switch/uplink, one → its cable/port)",
                    ),
                    tr(
                        "ケーブル・コネクタ・ノイズ源（インバータ等）との距離を確認する",
                        "Check cable, connectors and distance from noise sources (inverters)",
                    ),
                    tr(
                        "機器のCPU負荷が高いと応答が遅れることがあります",
                        "A busy device CPU can also delay replies",
                    ),
                ],
            )
        return (
            tr(f"{ip} から応答がありません。", f"No reply from {ip}."),
            [
                tr(
                    "機器の電源・ケーブル・IP設定を確認する",
                    "Check the device power, cabling and IP settings",
                ),
                tr(
                    "ルーター越しの場合は、途中のルーター/ファイアウォールでpingが遮断されていないか確認する",
                    "Across routers, check whether a router/firewall drops ping",
                ),
                oscmd(f"tracepath {ip}", f"tracert -d {ip}"),
            ],
        )


@register
class PortsProbe(Probe):
    name = "ports"
    title = Text("サービスポート", "Service ports")
    layer = "L4"
    scope = "target"
    depends_on = ["reach"]
    what = Text(
        "機器のサービスポート（Modbus 502、EtherNet/IP 44818、OPC UA 4840 など）にTCP接続できるか確認します。",
        "TCP-connects to the device's service ports (Modbus 502, EtherNet/IP 44818, OPC UA 4840 ...).",
    )
    why = Text(
        "pingが通っても通信アプリが動くとは限りません。『閉じている（機器は生きているが待ち受けなし）』と『無応答（FW/停止）』を区別します。",
        "Ping working does not mean the application works; 'closed' (alive, not listening) "
        "and 'no answer' (firewall/down) mean different fixes.",
    )
    manual_commands = [
        "Test-NetConnection <device-ip> -Port 502",
        "nc -vz <device-ip> 502",
    ]

    def check(self, ctx: Context) -> CheckResult:
        t = target(ctx)
        ports = [int(p) for p in (t.get("ports") or [])]
        if not ports:
            return CheckResult.skip(
                self.name,
                tr("確認するポートの指定なし", "no ports configured"),
                code="ports.none",
            )
        route = ctx.result("route")
        ip = (route.detail.get("address") if route is not None else None) or t.get(
            "host"
        )
        res = tcp_check_many(
            [(ip, p) for p in ports],
            attempts=int(ctx.get("tcp_attempts", 2)),
            timeout=float(ctx.get("tcp_timeout", 2.0)),
        )
        rows = [
            {"port": p, "service": SERVICE_NAMES.get(p), **res[(ip, p)].to_dict()}
            for p in ports
        ]
        detail = {"address": ip, "ports": rows}
        opened = [r for r in rows if r["state"] == "open"]
        closed = [r for r in rows if r["state"] == "closed"]
        silent = [r for r in rows if r["state"] not in ("open", "closed")]
        flaky = [r for r in opened if r["succeeded"] < r["attempts"]]

        def _fmt(r) -> str:
            lbl = port_label(r["port"])
            if r["state"] == "open":
                ms = r["connect_ms"]["median"] if r.get("connect_ms") else None
                return (
                    f"{lbl} "
                    + tr("開", "open")
                    + (f" {fmt_ms(ms)}" if ms is not None else "")
                )
            return f"{lbl} " + {
                "closed": tr("閉(拒否)", "closed"),
                "filtered": tr("応答なし", "no answer"),
                "unreachable": tr("到達不可", "unreachable"),
            }.get(r["state"], tr("エラー", "error"))

        summary = ", ".join(_fmt(r) for r in rows)
        metrics = {"ports_open": float(len(opened)), "ports_total": float(len(rows))}
        # Is there *any* sign of life? A reset (closed port) proves the host is
        # up as much as an open port does; so do ping replies or a fresh ARP
        # answer. Without any, "the port does not answer" would mislead.
        reach = ctx.result("reach")
        rdet = reach.detail if reach is not None else {}
        alive = bool(opened or closed) or bool(
            (rdet.get("ping") or {}).get("received") or rdet.get("l2") == "present"
        )
        detail["alive"] = alive
        if not alive:
            return CheckResult.fail(
                self.name,
                summary
                + tr(
                    " — pingにもTCPにも応答がありません",
                    " — no answer to ping or TCP at all",
                ),
                metrics=metrics,
                detail=detail,
                code="ports.no_response",
            )
        if closed or silent:
            code = "ports.closed" if closed and not silent else "ports.no_answer"
            return CheckResult.fail(
                self.name, summary, metrics=metrics, detail=detail, code=code
            )
        if flaky:
            return CheckResult.warn(
                self.name,
                summary
                + tr("（一部の接続試行が失敗）", " (some connection attempts failed)"),
                metrics=metrics,
                detail=detail,
                code="ports.flaky",
            )
        return CheckResult.ok(
            self.name, summary, metrics=metrics, detail=detail, code="ports.ok"
        )

    def advice(self, result, results=None):
        d = result.detail if result is not None else {}
        ip = d.get("address") or tr("<機器IP>", "<device-ip>")
        if result is not None and result.code == "ports.no_response":
            return (
                tr(
                    f"{ip} からは ping にも TCP にもまったく応答がありません。",
                    f"{ip} does not answer ping or TCP at all.",
                ),
                [
                    tr(
                        "機器の電源・LANケーブル・リンクランプを確認する",
                        "Check the device power, cable and link LEDs",
                    ),
                    tr(
                        f"機器側のIP設定が本当に {ip} か確認する",
                        f"Confirm the device is really configured as {ip}",
                    ),
                    tr(
                        "ルーター/ファイアウォールを経由する場合は、どこで止まるか確認する: "
                        + oscmd(f"tracepath {ip}", f"tracert -d {ip}"),
                        "Across routers/firewalls, find where it stops: "
                        + oscmd(f"tracepath {ip}", f"tracert -d {ip}"),
                    ),
                ],
            )
        rows = d.get("ports") or []
        closed = [port_label(r["port"]) for r in rows if r.get("state") == "closed"]
        silent = [
            port_label(r["port"])
            for r in rows
            if r.get("state") not in ("open", "closed")
        ]
        steps: List[str] = []
        if closed:
            steps.append(
                tr(
                    f"ポート {', '.join(closed)} は『閉』: 機器は応答していますがそのポートで待ち受けていません → 機器側でその通信機能（例: Modbus/TCP, MCプロトコル）が有効か・ポート番号の設定を確認",
                    f"Port(s) {', '.join(closed)} closed: the device answers but nothing listens → enable the protocol / check the port number in the device settings",
                )
            )
        if silent:
            steps.append(
                tr(
                    f"ポート {', '.join(silent)} は『応答なし』: 途中のファイアウォール/ルーター設定、または機器のアクセス制限（許可IP）を確認",
                    f"Port(s) {', '.join(silent)} silent: check firewalls/routers in between or the device's IP allow-list",
                )
            )
        steps.append(
            tr(
                "機器によっては同時接続数に上限があります。他のPC/ソフトが接続を占有していないか確認",
                "Some controllers allow only a few concurrent connections; check other PCs/software holding them",
            )
        )
        steps.append(
            oscmd(f"nc -vz {ip} <port>", f"Test-NetConnection {ip} -Port <port>")
        )
        return (
            tr(
                f"{ip} には届いていますが、サービスのポートに接続できません。",
                f"{ip} is reachable but its service port does not accept connections.",
            ),
            steps,
        )
