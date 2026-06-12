"""L1: Wi-Fi link quality.

'The internet is flaky' is often a weak radio link, not an upstream problem.
Signal strength and link-rate readings separate L1 from everything above it.
"""

from __future__ import annotations

import re
from typing import List, Optional

from ..core import CheckResult, Context, Probe, register
from ..core.util import have, is_linux, run

_SIGNAL_RE = re.compile(r"signal:\s*(-?\d+)\s*dBm")
_BITRATE_RE = re.compile(r"(?:tx|rx) bitrate:\s*([\d.]+)\s*MBit/s")
_SSID_RE = re.compile(r"SSID:\s*(.+)")


def _wifi_interfaces() -> List[str]:
    if not have("iw"):
        return []
    res = run(["iw", "dev"])
    if not res.ok:
        return []
    return re.findall(r"Interface\s+(\S+)", res.stdout)


@register
class WifiProbe(Probe):
    name = "wifi"
    title = "Wi-Fi link"
    layer = "L1"
    depends_on: List[str] = []
    what = "Reads Wi-Fi signal strength, SSID and link rate (if on wireless)."
    why = (
        "A weak/degrading radio link causes intermittent loss that looks like an "
        "upstream fault. Reading L1 directly tells you it's the air, not the WAN."
    )
    manual_commands = ["iw dev", "iw dev <iface> link", "nmcli -f SIGNAL,SSID dev wifi"]

    def check(self, ctx: Context) -> CheckResult:
        if not is_linux():
            return CheckResult.skip(self.name, "Wi-Fi inspection only on Linux")
        if not have("iw"):
            return CheckResult.skip(
                self.name, "'iw' command not installed (skipping Wi-Fi check)"
            )
        ifaces = _wifi_interfaces()
        if not ifaces:
            return CheckResult.skip(self.name, "no Wi-Fi interface present")

        worst: Optional[CheckResult] = None
        for iface in ifaces:
            res = run(["iw", "dev", iface, "link"])
            text = res.stdout
            if "Not connected" in text or not text.strip():
                r = CheckResult.warn(
                    self.name,
                    f"{iface}: not associated to any AP",
                    metrics={"associated": 0.0},
                )
                worst = r
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

            if signal is not None and signal <= -80:
                r = CheckResult.warn(
                    self.name, f"{desc} (weak signal)", metrics=metrics, detail=detail
                )
                worst = r
            elif worst is None:
                worst = CheckResult.ok(self.name, desc, metrics=metrics, detail=detail)
        return worst or CheckResult.skip(self.name, "no Wi-Fi link info")
