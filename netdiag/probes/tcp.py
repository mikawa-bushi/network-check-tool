"""L4: TCP connect test to specific service ports (``--tcp HOST:PORT``).

ICMP reachable but TCP refused/filtered is the classic 'firewall or service
down' signature. Pair this with the firewall probe to decide which.
"""

from __future__ import annotations

from typing import Iterable, List, Tuple

from ..core import CheckResult, Context, InputError, Probe, register
from ..i18n import Text, tr
from ..inventory import InventoryError, normalize_host, parse_ports
from ..measure import tcp_check_many
from ..measure.icmp import _literal_family
from ..measure.tcp import tcp_connect_once
from ._common import fmt_ms


def tcp_connect(host: str, port: int, timeout: float = 3.0) -> Tuple[bool, float, str]:
    """Return (connected, elapsed_ms, reason) — kept for API compatibility."""
    return tcp_connect_once(host, port, timeout)


def parse_target(spec: str) -> List[Tuple[str, int]]:
    """``host:port`` / ``[v6]:port`` / ``host:modbus,4840`` -> [(host, port)].

    The host is normalised like a ledger cell (``192.168.001.010`` is
    ``192.168.1.10``). Raises :class:`InputError` for a target without a
    port, or with a port or protocol netdiag cannot read.
    """
    text = spec.strip()
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        ports = rest[1:] if rest.startswith(":") else ""
    elif text.count(":") > 1:
        host, ports = "", ""
    else:
        host, _, ports = text.rpartition(":")
    try:
        if not host or not ports.strip():
            raise InventoryError(
                tr(
                    "ホスト:ポート の形で指定してください（例: 192.168.3.39:502、[2001:db8::1]:502）",
                    "write host:port (e.g. 192.168.3.39:502 or [2001:db8::1]:502)",
                )
            )
        host = normalize_host(host)
        return [(host, p) for p in parse_ports(ports)]
    except InventoryError as e:
        raise InputError(f"--tcp {spec}: {e}") from e


def parse_tcp_targets(specs: Iterable[str]) -> List[Tuple[str, int]]:
    """Every target in *specs*, or :class:`InputError` naming each bad one."""
    pairs: List[Tuple[str, int]] = []
    problems: List[str] = []
    for spec in specs:
        try:
            pairs.extend(p for p in parse_target(spec) if p not in pairs)
        except InputError as e:
            problems.append(str(e))
    if problems:
        raise InputError("\n".join(problems))
    return pairs


def format_target(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def state_text(state: str) -> str:
    return {
        "open": tr("開", "open"),
        "closed": tr("閉(拒否)", "closed (refused)"),
        "filtered": tr("応答なし", "no answer"),
        "unreachable": tr("到達不可", "unreachable"),
    }.get(state, tr("エラー", "error"))


@register
class TcpProbe(Probe):
    name = "tcp"
    title = Text("TCPポート", "TCP service port")
    layer = "L4"
    depends_on = ["interface", "dns"]
    what = Text(
        "host:port へTCP接続できるか確認します。",
        "Opens a TCP connection to host:port.",
    )
    why = Text(
        "サービス停止/ポート閉鎖とネットワーク断を区別します。pingは通るのにTCPが拒否/無応答なら、ファイアウォールかサービスの問題です。",
        "Distinguishes a blocked/closed service from a network outage. ICMP OK "
        "but TCP refused/filtered points at a firewall or a dead service.",
    )
    manual_commands = [
        "Test-NetConnection <host> -Port <port>",
        "nc -vz <host> <port>",
    ]

    def path_dependencies(self, ctx: Context) -> List[str]:
        """An IP address needs no DNS."""
        try:
            pairs = parse_tcp_targets(ctx.get("tcp_targets") or [])
        except InputError:
            return list(self.depends_on)
        if pairs and all(_literal_family(h) for h, _p in pairs):
            return [d for d in self.depends_on if d != "dns"]
        return list(self.depends_on)

    def check(self, ctx: Context) -> CheckResult:
        targets: List[str] = ctx.get("tcp_targets", [])
        if not targets:
            return CheckResult.skip(
                self.name,
                tr("TCPの確認対象がありません", "no TCP targets configured"),
                code="tcp.none",
            )
        try:
            pairs = parse_tcp_targets(targets)
        except InputError as e:
            return CheckResult.unknown(
                self.name, str(e), detail={"targets": targets}, code="tcp.bad_target"
            )
        results = tcp_check_many(
            pairs,
            attempts=int(ctx.get("tcp_attempts", 2)),
            timeout=float(ctx.get("tcp_timeout", 3.0)),
        )
        rows = []
        for (host, port), pc in results.items():
            ms = min(pc.times_ms) if pc.times_ms else None
            rows.append(
                {
                    "target": f"{host}:{port}",
                    "connected": pc.is_open,
                    "state": pc.state,
                    "ms": round(ms, 1) if ms is not None else None,
                    "reason": ", ".join(pc.reasons) if pc.reasons else "connected",
                }
            )
        detail = {"targets": rows}
        ok_count = sum(1 for r in rows if r["connected"])
        total = len(rows)
        summary = ", ".join(
            f"{r['target']} "
            + (f"OK {fmt_ms(r['ms'])}" if r["connected"] else state_text(r["state"]))
            for r in rows
        )
        metrics = {"connect_ok": float(ok_count), "connect_total": float(total)}
        if total and ok_count == total:
            return CheckResult.ok(
                self.name, summary, metrics=metrics, detail=detail, code="tcp.ok"
            )
        if ok_count == 0:
            return CheckResult.fail(
                self.name, summary, metrics=metrics, detail=detail, code="tcp.failed"
            )
        return CheckResult.warn(
            self.name, summary, metrics=metrics, detail=detail, code="tcp.partial"
        )

    def advice(self, result, results=None):
        return (
            tr(
                "相手には届いていますが、サービスのポートに接続できません。",
                "The host resolves and is reachable, but the service port won't connect.",
            ),
            [
                tr(
                    "『閉(拒否)』= 相手は生きているがそのポートで待ち受けていない → 機器側のサービス/設定を確認",
                    "`refused` = nothing listening: check the service/setting on the device",
                ),
                tr(
                    "『応答なし』= ファイアウォールで破棄されているか機器が停止 → 経路上のFW・機器の電源を確認",
                    "`no answer` = dropped by a firewall or the host is down",
                ),
                tr(
                    "サーバー側で待ち受け確認: netstat -an / ss -tlnp",
                    "Confirm the listener on the server: netstat -an / ss -tlnp",
                ),
            ],
        )
