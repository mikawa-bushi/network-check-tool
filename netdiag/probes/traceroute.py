"""L3 path analysis: where along the path do packets stop?

ping answers reachable/not; traceroute answers *where* it breaks — your own
router, the ISP's third hop, or further out — which is what decides whether the
user reboots the router or calls the ISP.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import List, Optional, Set

from ..core import CheckResult, Context, Probe, register
from ..core.util import have, run
from ..i18n import Text, tr

# Matches both traceroute ("  1  10.0.0.1 ...") and tracepath (" 1:  10.0.0.1 ...");
# the optional "?:" handles tracepath's localhost/pmtu discovery line.
_HOP_RE = re.compile(r"^\s*(\d+)\??:?\s+(.*)$")
_MS_RE = re.compile(r"([\d.]+)\s*ms")
TIMEOUT_S = 60


def _hop_ip(rest: str) -> Optional[str]:
    """The first IPv4 or IPv6 address on a hop line."""
    for token in re.split(r"[\s()\[\]]+", rest):
        try:
            return str(ipaddress.ip_address(token.split("%", 1)[0]))
        except ValueError:
            continue
    return None


def _addresses_of(host: str) -> Set[str]:
    """Every address *host* names, so a hop can be matched to a host name."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return {host}
    return {str(ipaddress.ip_address(i[4][0].split("%", 1)[0])) for i in infos}


def _parse_hops(text: str) -> List[dict]:
    # tracepath prints several lines per hop; collapse to one entry per hop
    # number, preferring a line that actually resolved an IP.
    by_hop: dict = {}
    order: List[int] = []
    for line in text.splitlines():
        m = _HOP_RE.match(line)
        if not m:
            continue
        rest = m.group(2)
        if "pmtu" in rest or "[LOCALHOST]" in rest:
            continue
        n = int(m.group(1))
        ip = _hop_ip(rest)
        ms = [float(x) for x in _MS_RE.findall(rest)]
        rec = {
            "hop": n,
            "ip": ip,
            "rtt_ms": round(sum(ms) / len(ms), 2) if ms else None,
            "timeout": "*" in rest and not ip,
        }
        if n not in by_hop:
            order.append(n)
            by_hop[n] = rec
        elif by_hop[n]["ip"] is None and rec["ip"] is not None:
            by_hop[n] = rec  # upgrade a timed-out hop to a resolved one
    return [by_hop[n] for n in order]


@register
class TracerouteProbe(Probe):
    name = "traceroute"
    title = Text("経路トレース", "Path trace")
    layer = "L3"
    depends_on = ["gateway"]
    requires_root = False
    default = False  # slow and noisy against hosts that filter it: on demand only
    what = Text(
        "宛先までの経路を1ホップずつ調べ、どこで止まるかを確認します。",
        "Traces the hop-by-hop path to a destination and where it stops.",
    )
    why = Text(
        "故障区間を特定します。1ホップ目で止まれば手元のルーター、途中で止まればその先の区間が原因です。",
        "Pinpoints the failing segment. Stopping at hop 1 implicates your router; "
        "stopping mid-path implicates a specific upstream/ISP segment.",
    )
    manual_commands = [
        "traceroute -n -w2 -q1 <host>",
        "tracepath <host>",
    ]
    means = Text(
        "経路の途中で通信が止まっています。", "Traffic stops somewhere along the path."
    )
    runbook = [
        Text(
            "最後に応答したホップの次の機器（ルーター/L3スイッチ/回線）を確認する",
            "Check the device right after the last responding hop.",
        )
    ]

    def check(self, ctx: Context) -> CheckResult:
        host = ctx.get("trace_target", ctx.get("external_ip", "1.1.1.1"))
        max_hops = int(ctx.get("max_hops", 20))

        if have("traceroute"):
            res = run(
                ["traceroute", "-n", "-w", "2", "-q", "1", "-m", str(max_hops), host],
                timeout=TIMEOUT_S,
            )
        elif have("tracepath"):
            res = run(["tracepath", "-n", "-m", str(max_hops), host], timeout=TIMEOUT_S)
        else:
            return CheckResult.unknown(
                self.name,
                tr(
                    "traceroute/tracepath がありません",
                    "no traceroute/tracepath command",
                ),
                code="traceroute.no_tool",
            )

        if not res.found:
            return CheckResult.unknown(
                self.name,
                tr(
                    "traceroute コマンドがありません",
                    "traceroute command not available",
                ),
                code="traceroute.no_tool",
            )

        hops = _parse_hops(res.stdout or res.stderr)
        detail = {
            "target": host,
            "hops": hops,
            "output": res.stdout,
            "timed_out": res.timed_out,
        }
        if not hops:
            return CheckResult.unknown(
                self.name,
                tr(
                    "traceroute の出力を解析できません",
                    "could not parse traceroute output",
                ),
                detail=detail,
                code="traceroute.unparsed",
            )

        wanted = _addresses_of(host) | {host}
        reached = any(h["ip"] in wanted for h in hops)
        last_ok = None
        for h in hops:
            if h["ip"] and not h["timeout"]:
                last_ok = h
        trailing_timeouts = 0
        for h in reversed(hops):
            if h["timeout"] or not h["ip"]:
                trailing_timeouts += 1
            else:
                break

        metrics = {"hop_count": float(len(hops)), "reached": 1.0 if reached else 0.0}
        if reached:
            return CheckResult.ok(
                self.name,
                tr(
                    f"{host} に {len(hops)} ホップで到達",
                    f"reached {host} in {len(hops)} hops",
                ),
                metrics=metrics,
                detail=detail,
                code="traceroute.ok",
            )
        if res.timed_out:
            last = (
                tr(
                    f"ホップ {last_ok['hop']} ({last_ok['ip']}) まで応答",
                    f"last answer from hop {last_ok['hop']} ({last_ok['ip']})",
                )
                if last_ok
                else tr("応答したホップなし", "no hop answered")
            )
            return CheckResult.unknown(
                self.name,
                tr(
                    f"{host} への経路トレースが {TIMEOUT_S} 秒で打ち切られました（{last}）",
                    f"the trace to {host} was cut off after {TIMEOUT_S} s ({last})",
                ),
                metrics=metrics,
                detail=detail,
                code="traceroute.timeout",
            )
        if last_ok:
            return CheckResult.warn(
                self.name,
                tr(
                    f"{host} への経路はホップ {last_ok['hop']} ({last_ok['ip']}) の後で途切れています（以降 {trailing_timeouts} ホップ無応答）",
                    f"path to {host} stops after hop {last_ok['hop']} "
                    f"({last_ok['ip']}); {trailing_timeouts} trailing timeout(s)",
                ),
                metrics=metrics,
                detail=detail,
                code="traceroute.stopped",
            )
        return CheckResult.fail(
            self.name,
            tr(
                f"{host} 方向のホップが1つも応答しません",
                f"no hops responded toward {host}",
            ),
            metrics=metrics,
            detail=detail,
            code="traceroute.no_hops",
        )
