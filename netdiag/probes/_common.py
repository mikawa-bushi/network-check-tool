"""Helpers shared by the built-in probes: OS-aware commands and number formats."""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..core.util import os_name
from ..i18n import tr
from ..measure import PingStats, precise_ping_available

# LAN latency guide values. A healthy switched LAN answers in well under a
# millisecond; PLC Ethernet modules often answer ICMP in a low-priority task
# (a few ms). Above these, cyclic communication starts to feel it.
LAN_RTT_WARN_MS = 10.0  # median
LAN_RTT_P95_WARN_MS = 50.0
WAN_RTT_WARN_MS = 150.0


def oscmd(linux: str, windows: str, macos: Optional[str] = None) -> str:
    """Pick the manual command that matches this OS (for runbook steps)."""
    name = os_name()
    if name == "windows":
        return windows
    if name == "macos":
        return macos or linux
    return linux


def default_ping_count() -> int:
    """10 fast samples with a precise pinger; 5 with ping.exe's 1 s cadence."""
    return 10 if precise_ping_available() else 5


def ping_settings(ctx_get) -> Dict[str, Any]:
    """count/interval/timeout from a probe context (``ctx.get``)."""
    return {
        "count": int(ctx_get("count") or default_ping_count()),
        "interval": float(ctx_get("interval") or 0.2),
        "timeout": float(ctx_get("ping_timeout") or 1.0),
    }


def fmt_ms(v: Optional[float]) -> str:
    if v is None:
        return "—"
    if v < 1:
        return f"{v:.2f}ms"
    if v < 10:
        return f"{v:.1f}ms"
    return f"{v:.0f}ms"


def fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "—"
    if v == 0:
        return "0%"
    if v < 1:
        return f"{v:.2f}%"
    return f"{v:.1f}%" if v < 10 else f"{v:.0f}%"


def ping_summary(st: PingStats) -> str:
    """One-line statistics: replies, loss (95% upper bound), median/p95/max RTT."""
    sep = tr("・", ", ")
    parts = [tr(f"応答 {st.received}/{st.sent}", f"{st.received}/{st.sent} replies")]
    if st.sent:
        loss = tr(f"損失 {fmt_pct(st.loss_pct)}", f"loss {fmt_pct(st.loss_pct)}")
        upper = st.loss_upper_pct
        if st.lost == 0 and upper is not None:
            loss += tr(
                f"（95%上限 {fmt_pct(upper)}）", f" (95% upper {fmt_pct(upper)})"
            )
        parts.append(loss)
    r = st.rtt
    if r is not None:
        parts.append(
            tr(
                f"RTT 中央 {fmt_ms(r.median)} / p95 {fmt_ms(r.p95)} / 最大 {fmt_ms(r.max)}",
                f"RTT median {fmt_ms(r.median)} / p95 {fmt_ms(r.p95)} / max {fmt_ms(r.max)}",
            )
        )
        if r.count > 2:
            parts.append(tr(f"ジッタ {fmt_ms(r.jitter)}", f"jitter {fmt_ms(r.jitter)}"))
    if st.duplicates:
        parts.append(tr(f"重複応答 {st.duplicates}", f"{st.duplicates} DUP"))
    return sep.join(parts)


def classify_latency(st: PingStats, on_link: bool) -> Optional[str]:
    """A latency complaint (localised) or None when RTTs look normal."""
    r = st.rtt
    if r is None:
        return None
    if on_link:
        if r.median > LAN_RTT_WARN_MS:
            return tr(
                f"同一LANとしては応答が遅い (中央値 {fmt_ms(r.median)})",
                f"slow for a local LAN (median {fmt_ms(r.median)})",
            )
        if r.p95 > LAN_RTT_P95_WARN_MS and r.count >= 5:
            return tr(
                f"時々大きな遅延 (p95 {fmt_ms(r.p95)}, 最大 {fmt_ms(r.max)})",
                f"intermittent latency spikes (p95 {fmt_ms(r.p95)}, max {fmt_ms(r.max)})",
            )
    elif r.median > WAN_RTT_WARN_MS:
        return tr(
            f"応答が遅い (中央値 {fmt_ms(r.median)})",
            f"slow responses (median {fmt_ms(r.median)})",
        )
    return None
