"""SYSTEM layer: is the fault actually the OS, not the network?

Resource exhaustion, socket/conntrack exhaustion, and a wrong clock all present
as 'network problems'. Checking the host itself catches the cases where the
network is innocent. Pure stdlib — reads /proc and a couple of optional commands.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from ..core import CheckResult, Context, Probe, register
from ..core.util import have, is_linux, run


def _loadavg() -> Optional[float]:
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):
        return None


def _mem_available_pct() -> Optional[float]:
    p = Path("/proc/meminfo")
    if not p.exists():
        return None
    try:
        info: Dict[str, int] = {}
        for line in p.read_text().splitlines():
            k, _, v = line.partition(":")
            info[k.strip()] = int(v.strip().split()[0])
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        if total:
            return avail / total * 100.0
    except (OSError, ValueError):
        pass
    return None


def _conntrack() -> Optional[Dict[str, int]]:
    count_p = Path("/proc/sys/net/netfilter/nf_conntrack_count")
    max_p = Path("/proc/sys/net/netfilter/nf_conntrack_max")
    try:
        if count_p.exists() and max_p.exists():
            return {"count": int(count_p.read_text()), "max": int(max_p.read_text())}
    except (OSError, ValueError):
        pass
    return None


@register
class SystemProbe(Probe):
    name = "system"
    title = "Host resources"
    layer = "SYSTEM"
    depends_on: List[str] = []
    what = "Checks CPU load, available memory, and connection-tracking table usage."
    why = (
        "Exhausted memory or a full conntrack table drops connections in ways that "
        "look exactly like network faults. This catches the OS-side root cause."
    )
    manual_commands = [
        "uptime", "cat /proc/meminfo",
        "cat /proc/sys/net/netfilter/nf_conntrack_count "
        "/proc/sys/net/netfilter/nf_conntrack_max",
    ]

    def check(self, ctx: Context) -> CheckResult:
        if not is_linux():
            return CheckResult.skip(self.name, "host metrics only implemented on Linux")
        issues: List[str] = []
        metrics: Dict[str, float] = {}
        detail: Dict = {}

        load = _loadavg()
        ncpu = os.cpu_count() or 1
        if load is not None:
            metrics["load1"] = load
            metrics["load_per_cpu"] = load / ncpu
            detail["loadavg1"] = load
            detail["cpus"] = ncpu
            if load / ncpu > 4:
                issues.append(f"very high CPU load ({load:.1f} over {ncpu} CPUs)")

        mem = _mem_available_pct()
        if mem is not None:
            metrics["mem_available_pct"] = mem
            detail["mem_available_pct"] = round(mem, 1)
            if mem < 5:
                issues.append(f"memory nearly exhausted ({mem:.0f}% available)")

        ct = _conntrack()
        if ct:
            usage = ct["count"] / ct["max"] * 100.0 if ct["max"] else 0
            metrics["conntrack_pct"] = usage
            detail["conntrack"] = ct
            if usage > 90:
                issues.append(
                    f"conntrack table {usage:.0f}% full ({ct['count']}/{ct['max']}) "
                    f"— new connections may be dropped"
                )

        if issues:
            return CheckResult.warn(self.name, "; ".join(issues),
                                    metrics=metrics, detail=detail)
        return CheckResult.ok(
            self.name,
            f"load {load:.2f}/{ncpu}cpu, mem {mem:.0f}% free" if load and mem
            else "host resources nominal",
            metrics=metrics, detail=detail,
        )


@register
class ClockProbe(Probe):
    name = "clock"
    title = "System clock"
    layer = "SYSTEM"
    depends_on: List[str] = []
    what = "Checks whether the system clock is synchronised (NTP)."
    why = "A skewed clock breaks TLS validation and Kerberos/auth, mimicking network faults."
    manual_commands = ["timedatectl status", "chronyc tracking"]

    def check(self, ctx: Context) -> CheckResult:
        if have("timedatectl"):
            res = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
            if res.ok:
                synced = res.stdout.strip() == "yes"
                if synced:
                    return CheckResult.ok(self.name, "clock NTP-synchronised",
                                          metrics={"ntp_synced": 1.0})
                return CheckResult.warn(
                    self.name, "clock NOT NTP-synchronised (may break TLS/auth)",
                    metrics={"ntp_synced": 0.0},
                )
        return CheckResult.skip(self.name, "timedatectl not available")
