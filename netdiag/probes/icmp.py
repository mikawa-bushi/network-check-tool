"""ICMP reachability via the system ``ping`` command (no raw socket / no root).

Using the ``ping`` binary instead of a raw socket is a deliberate choice: it
keeps netdiag runnable as an unprivileged user, which matters for a tool meant to
work anywhere with minimal setup.
"""

from __future__ import annotations

import re
from typing import Callable, Optional, Tuple, Union

from ..core import CheckResult, Context, Probe, register
from ..core.util import have, is_windows, run

_RTT_RE = re.compile(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)")  # min/avg/max
_LOSS_RE = re.compile(r"([\d.]+)%\s*packet loss")
_WIN_LOSS_RE = re.compile(r"\((\d+)%\s*loss\)")
_WIN_RTT_RE = re.compile(r"Average\s*=\s*(\d+)ms")

# Shared loss thresholds for ping-based probes: total loss is a failure, any
# loss is a warning, none is OK. Defined once so every ping probe agrees.
FAIL_LOSS_PCT = 100.0
WARN_LOSS_PCT = 0.0

# A status message may be a plain string or a no-arg callable, so callers can
# defer RTT formatting to the warn/ok branches (where RTT is present) and pass a
# plain string for the failure branch (where RTT is typically None).
_Msg = Union[str, Callable[[], str]]


def classify_ping(
    name: str,
    rtt: Optional[float],
    loss: float,
    text: str,
    *,
    fail_msg: _Msg,
    warn_msg: _Msg,
    ok_msg: _Msg,
    extra_metrics: Optional[dict] = None,
    extra_detail: Optional[dict] = None,
) -> CheckResult:
    """Turn ``ping()`` output into a CheckResult using the shared loss thresholds.

    Centralises the ``>=100% -> fail`` / ``>0% -> warn`` / ``else -> ok`` cascade
    and the metric/detail assembly so each ping probe only supplies its own
    host-specific wording. ``extra_metrics``/``extra_detail`` merge in per-probe
    fields (e.g. the gateway probe's ``has_gateway``/``gateway``).
    """
    metrics = {"loss_pct": loss}
    if rtt is not None:
        metrics["rtt_ms"] = rtt
    if extra_metrics:
        metrics.update(extra_metrics)
    detail = {"output": text}
    if extra_detail:
        detail.update(extra_detail)

    if loss >= FAIL_LOSS_PCT:
        msg, factory = fail_msg, CheckResult.fail
    elif loss > WARN_LOSS_PCT:
        msg, factory = warn_msg, CheckResult.warn
    else:
        msg, factory = ok_msg, CheckResult.ok
    return factory(
        name, msg() if callable(msg) else msg, metrics=metrics, detail=detail
    )


def ping(
    host: str, count: int = 4, timeout: float = 2.0, family: Optional[int] = None
) -> Tuple[Optional[float], float, str]:
    """Return (avg_rtt_ms or None, loss_pct, raw_output).

    ``family`` forces the IP family: ``4`` adds ``-4`` and ``6`` adds ``-6`` (the
    dual-stack probe uses this so both families share one parser and arg builder).
    """
    flag = ["-4"] if family == 4 else (["-6"] if family == 6 else [])
    if is_windows():
        args = ["ping", *flag, "-n", str(count), "-w", str(int(timeout * 1000)), host]
    else:
        args = ["ping", *flag, "-c", str(count), "-W", str(int(max(timeout, 1))), host]
    res = run(args, timeout=count * timeout + 5)
    text = res.stdout or res.stderr

    loss = 100.0
    m = _LOSS_RE.search(text) or _WIN_LOSS_RE.search(text)
    if m:
        loss = float(m.group(1))

    rtt: Optional[float] = None
    m = _RTT_RE.search(text)
    if m:
        rtt = float(m.group(2))
    else:
        m = _WIN_RTT_RE.search(text)
        if m:
            rtt = float(m.group(1))
    return rtt, loss, text


@register
class ExternalProbe(Probe):
    """Reachability of a stable off-LAN IP address.

    Defaults to an IP (not a hostname) so this stays independent of DNS — that
    separation is exactly what lets the engine distinguish a routing/WAN outage
    from a name-resolution problem. In a closed network, point ``external_ip`` at
    an in-site address that should always answer.
    """

    name = "external"
    title = "External reachability"
    layer = "L3"
    depends_on = ["gateway"]
    what = "Pings a fixed off-LAN IP address (no DNS involved)."
    why = (
        "Separates 'can packets leave the LAN and come back' from 'can we resolve "
        "names'. If this works but DNS fails, the fault is DNS, not the WAN."
    )
    manual_commands = ["ping -c4 1.1.1.1"]

    def check(self, ctx: Context) -> CheckResult:
        host = ctx.get("external_ip", "1.1.1.1")
        count = int(ctx.get("count", 4))
        if not have("ping"):
            return CheckResult.skip(self.name, "ping command not available")
        rtt, loss, text = ping(host, count=count)
        return classify_ping(
            self.name,
            rtt,
            loss,
            text,
            fail_msg=f"{host} unreachable (100% loss) — WAN/upstream or routing problem",
            warn_msg=lambda: f"{host} reachable but {loss:.0f}% loss, avg {rtt:.1f}ms",
            ok_msg=lambda: f"{host} reachable ({rtt:.1f}ms, 0% loss)",
        )


@register
class PingProbe(Probe):
    """Ad-hoc ping of a user-supplied target (used by ``netdiag ping``)."""

    name = "icmp"
    title = "ICMP ping"
    layer = "L3"
    depends_on = ["gateway"]
    what = "Pings an arbitrary target host."
    why = "Measures raw reachability, RTT and packet loss to a chosen destination."
    manual_commands = ["ping -c4 <host>"]

    def check(self, ctx: Context) -> CheckResult:
        host = ctx.get("target")
        if not host:
            return CheckResult.skip(self.name, "no target specified")
        if not have("ping"):
            return CheckResult.skip(self.name, "ping command not available")
        rtt, loss, text = ping(host, count=int(ctx.get("count", 4)))

        def _summary() -> str:
            return f"{host}: {rtt:.1f}ms avg, {loss:.0f}% loss"

        return classify_ping(
            self.name,
            rtt,
            loss,
            text,
            fail_msg=f"{host}: 100% loss",
            warn_msg=_summary,
            ok_msg=_summary,
        )
