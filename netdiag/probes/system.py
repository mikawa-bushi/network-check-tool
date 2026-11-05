"""SYSTEM layer: is the fault actually the OS, not the network?

Resource exhaustion, socket/conntrack exhaustion, and a wrong clock all present
as 'network problems'. Checking the host itself catches the cases where the
network is innocent. Pure stdlib — reads /proc (Linux) or the Win32 API.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional

from ..core import CheckResult, Context, Probe, register
from ..core.util import have, is_linux, is_windows, run
from ..i18n import Text, tr


def _loadavg() -> Optional[float]:
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):
        return None


def _mem_available_pct() -> Optional[float]:
    if is_windows():
        return _windows_mem_available_pct()
    p = Path("/proc/meminfo")
    if not p.exists():
        return None
    try:
        info: Dict[str, int] = {}
        for line in p.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition(":")
            info[k.strip()] = int(v.strip().split()[0])
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        if total:
            return avail / total * 100.0
    except (OSError, ValueError):
        pass
    return None


def _windows_mem_available_pct() -> Optional[float]:
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if (
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            and st.ullTotalPhys
        ):
            return st.ullAvailPhys / st.ullTotalPhys * 100.0
    except (AttributeError, OSError):
        pass
    return None


def _conntrack() -> Optional[Dict[str, int]]:
    count_p = Path("/proc/sys/net/netfilter/nf_conntrack_count")
    max_p = Path("/proc/sys/net/netfilter/nf_conntrack_max")
    try:
        if count_p.exists() and max_p.exists():
            return {
                "count": int(count_p.read_text(encoding="utf-8")),
                "max": int(max_p.read_text(encoding="utf-8")),
            }
    except (OSError, ValueError):
        pass
    return None


@register
class SystemProbe(Probe):
    name = "system"
    title = Text("PCのリソース", "Host resources")
    layer = "SYSTEM"
    depends_on: List[str] = []
    what = Text(
        "CPU負荷・空きメモリ・コネクション追跡表の使用率を確認します。",
        "Checks CPU load, available memory, and connection-tracking table usage.",
    )
    why = Text(
        "メモリ不足や接続表の溢れは、ネットワーク障害とそっくりの症状を起こします。PC側の原因を拾います。",
        "Exhausted memory or a full conntrack table drops connections in ways that "
        "look exactly like network faults. This catches the OS-side root cause.",
    )
    manual_commands = [
        "uptime ; free -h",
        "cat /proc/sys/net/netfilter/nf_conntrack_count /proc/sys/net/netfilter/nf_conntrack_max",
        "taskmgr (Windows)",
    ]
    means = Text(
        "ネットワークではなくPC側のリソース不足が通信を劣化させています。",
        "An OS-side resource problem is degrading the network, not the network itself.",
    )
    runbook = [
        Text(
            "メモリとCPU負荷を確認する（free -h / uptime / タスクマネージャー）",
            "Check memory and load (`free -h`, `uptime`, Task Manager).",
        ),
        Text(
            "conntrack が満杯なら net.netfilter.nf_conntrack_max を増やすか、接続が漏れている原因を探す",
            "If conntrack is full, raise `net.netfilter.nf_conntrack_max` or find the leak.",
        ),
    ]

    def check(self, ctx: Context) -> CheckResult:
        if not (is_linux() or is_windows()):
            return CheckResult.skip(
                self.name,
                tr(
                    "このOSではリソース確認に未対応",
                    "host metrics only implemented on Linux/Windows",
                ),
                code="system.unsupported",
            )
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
                issues.append(
                    tr(
                        f"CPU負荷が非常に高い（{ncpu}コアに対し {load:.1f}）",
                        f"very high CPU load ({load:.1f} over {ncpu} CPUs)",
                    )
                )

        mem = _mem_available_pct()
        if mem is not None:
            metrics["mem_available_pct"] = mem
            detail["mem_available_pct"] = round(mem, 1)
            if mem < 5:
                issues.append(
                    tr(
                        f"メモリがほぼ枯渇（空き {mem:.0f}%）",
                        f"memory nearly exhausted ({mem:.0f}% available)",
                    )
                )

        ct = _conntrack()
        if ct:
            usage = ct["count"] / ct["max"] * 100.0 if ct["max"] else 0
            metrics["conntrack_pct"] = usage
            detail["conntrack"] = ct
            if usage > 90:
                issues.append(
                    tr(
                        f"conntrack表が {usage:.0f}% 使用（{ct['count']}/{ct['max']}）— 新しい接続が落ちる可能性",
                        f"conntrack table {usage:.0f}% full ({ct['count']}/{ct['max']}) "
                        f"— new connections may be dropped",
                    )
                )

        if issues:
            return CheckResult.warn(
                self.name,
                "; ".join(issues),
                metrics=metrics,
                detail=detail,
                code="system.resources",
            )
        parts = []
        if load is not None:
            parts.append(
                tr(f"負荷 {load:.2f}/{ncpu}コア", f"load {load:.2f}/{ncpu}cpu")
            )
        if mem is not None:
            parts.append(tr(f"空きメモリ {mem:.0f}%", f"mem {mem:.0f}% free"))
        return CheckResult.ok(
            self.name,
            ", ".join(parts) or tr("リソースは正常", "host resources nominal"),
            metrics=metrics,
            detail=detail,
            code="system.ok",
        )


@register
class ClockProbe(Probe):
    name = "clock"
    title = Text("PCの時計", "System clock")
    layer = "SYSTEM"
    depends_on: List[str] = []
    what = Text(
        "PCの時計がNTPで同期しているか確認します。",
        "Checks whether the system clock is synchronised (NTP).",
    )
    why = Text(
        "時計がずれるとTLS証明書の検証や認証が失敗し、ネットワーク障害のように見えます。ログの時刻照合も狂います。",
        "A skewed clock breaks TLS validation and Kerberos/auth, mimicking network faults.",
    )
    manual_commands = ["timedatectl status", "w32tm /query /status"]
    means = Text(
        "PCの時計が同期していません。TLSや認証が失敗し、機器ログとの時刻照合もずれます。",
        "The system clock is not synchronised, which can break TLS and auth.",
    )
    runbook = [
        Text(
            "NTPを有効化: sudo timedatectl set-ntp true / w32tm /resync",
            "Enable NTP: `sudo timedatectl set-ntp true` / `w32tm /resync`.",
        ),
    ]

    def check(self, ctx: Context) -> CheckResult:
        if have("timedatectl"):
            res = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
            if res.ok:
                synced = res.stdout.strip() == "yes"
                if synced:
                    return CheckResult.ok(
                        self.name,
                        tr("時計はNTP同期済み", "clock NTP-synchronised"),
                        metrics={"ntp_synced": 1.0},
                        code="clock.ok",
                    )
                return CheckResult.warn(
                    self.name,
                    tr(
                        "時計がNTP同期していません（TLS/認証・ログ照合に影響）",
                        "clock NOT NTP-synchronised (may break TLS/auth)",
                    ),
                    metrics={"ntp_synced": 0.0},
                    code="clock.unsynced",
                )
        return CheckResult.skip(
            self.name,
            tr("時刻同期の状態を取得できません", "timedatectl not available"),
            code="clock.unavailable",
        )
