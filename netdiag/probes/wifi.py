"""L1: Wi-Fi link quality.

'The network is flaky' is often a weak radio link (AGVs, tablets and handheld
scanners on the shop floor), not an upstream problem. Signal strength and
link-rate readings separate L1 from everything above it.
"""

from __future__ import annotations

import re
from typing import List, Optional

from ..core import CheckResult, Context, Probe, register
from ..core.util import have, is_linux, run
from ..i18n import Text, tr

_SIGNAL_RE = re.compile(r"signal:\s*(-?\d+)\s*dBm")
_BITRATE_RE = re.compile(r"(?:tx|rx) bitrate:\s*([\d.]+)\s*MBit/s")
_SSID_RE = re.compile(r"SSID:\s*(.+)")
WEAK_DBM = -75


def _wifi_interfaces() -> List[str]:
    if not have("iw"):
        return []
    res = run(["iw", "dev"])
    if not res.ok:
        return []
    return re.findall(r"Interface\s+(\S+)", res.stdout)


def _no_link(name: str, idle: List[str]) -> CheckResult:
    """No adapter is associated, so Wi-Fi is not this PC's path.

    A PC that needs Wi-Fi and lost it has no usable adapter, which the
    ``interface`` check reports as a failure.
    """
    if not idle:
        return CheckResult.skip(
            name, tr("Wi-Fi情報なし", "no Wi-Fi link info"), code="wifi.none"
        )
    names = ", ".join(idle)
    return CheckResult.skip(
        name,
        tr(
            f"{names}: アクセスポイント未接続（Wi-Fiは使用していません）",
            f"{names}: not associated to any AP (Wi-Fi not in use)",
        ),
        metrics={"associated": 0.0},
        code="wifi.not_associated",
    )


@register
class WifiProbe(Probe):
    name = "wifi"
    title = Text("Wi-Fi接続", "Wi-Fi link")
    layer = "L1"
    depends_on: List[str] = []
    what = Text(
        "無線LANの電波強度・SSID・リンク速度を確認します（無線接続時のみ）。",
        "Reads Wi-Fi signal strength, SSID and link rate (if on wireless).",
    )
    why = Text(
        "電波が弱い・不安定だと断続的なパケットロスが起き、上位の障害のように見えます。",
        "A weak/degrading radio link causes intermittent loss that looks like an "
        "upstream fault. Reading L1 directly tells you it's the air, not the WAN.",
    )
    manual_commands = ["iw dev <iface> link"]
    means = Text(
        "無線LANの電波が弱く、通信が不安定になります。",
        "The Wi-Fi radio link is weak, so traffic over it is unreliable.",
    )
    runbook = [
        Text(
            "アクセスポイントに近づく・遮蔽物（金属棚・設備）を避ける・チャネル干渉を確認する",
            "Move closer to the AP / reduce interference; reconnect.",
        )
    ]

    def check(self, ctx: Context) -> CheckResult:
        if not is_linux():
            return CheckResult.skip(
                self.name,
                tr(
                    "Wi-Fi確認はLinuxのみ対応",
                    "Wi-Fi inspection only on Linux",
                ),
                code="wifi.unsupported",
            )
        if not have("iw"):
            return CheckResult.skip(
                self.name,
                tr(
                    "'iw' コマンドがありません（Wi-Fi確認を省略）",
                    "'iw' command not installed (skipping Wi-Fi check)",
                ),
                code="wifi.no_tool",
            )
        ifaces = _wifi_interfaces()
        if not ifaces:
            return CheckResult.skip(
                self.name,
                tr("無線LANアダプタなし", "no Wi-Fi interface present"),
                code="wifi.none",
            )

        worst: Optional[CheckResult] = None
        idle: List[str] = []
        for iface in ifaces:
            res = run(["iw", "dev", iface, "link"])
            text = res.stdout
            if "Not connected" in text or not text.strip():
                idle.append(iface)
                continue
            sig = _SIGNAL_RE.search(text)
            rate = _BITRATE_RE.search(text)
            ssid = _SSID_RE.search(text)
            signal = int(sig.group(1)) if sig else None
            metrics = {"associated": 1.0}
            if signal is not None:
                metrics["signal_dbm"] = float(signal)
            if rate:
                metrics["bitrate_mbps"] = float(rate.group(1))
            ssid_s = ssid.group(1).strip() if ssid else "?"
            detail = {"iface": iface, "raw": text}
            desc = f"{iface} → {ssid_s}"
            if signal is not None:
                desc += f", {signal} dBm"
            if rate:
                desc += f", {rate.group(1)} Mbit/s"
            if signal is not None and signal <= WEAK_DBM:
                worst = CheckResult.warn(
                    self.name,
                    desc + tr("（電波が弱い）", " (weak signal)"),
                    metrics=metrics,
                    detail=detail,
                    code="wifi.weak",
                )
            elif worst is None:
                worst = CheckResult.ok(
                    self.name, desc, metrics=metrics, detail=detail, code="wifi.ok"
                )
        return worst or _no_link(self.name, idle)
