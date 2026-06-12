"""L4: TCP connect test to a specific service port.

ICMP reachable but TCP refused/filtered is the classic 'firewall or service
down' signature. Pair this with the firewall probe to decide which.
"""

from __future__ import annotations

import socket
import time
from typing import List, Tuple

from ..core import CheckResult, Context, Probe, register


def tcp_connect(host: str, port: int, timeout: float = 3.0) -> Tuple[bool, float, str]:
    """Return (connected, elapsed_ms, reason)."""
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, (time.perf_counter() - start) * 1000.0, "connected"
    except socket.timeout:
        return False, (time.perf_counter() - start) * 1000.0, "timeout (filtered?)"
    except ConnectionRefusedError:
        return False, (time.perf_counter() - start) * 1000.0, "connection refused"
    except socket.gaierror as e:
        return False, 0.0, f"name resolution failed: {e}"
    except OSError as e:
        return False, (time.perf_counter() - start) * 1000.0, str(e)


@register
class TcpProbe(Probe):
    name = "tcp"
    title = "TCP service port"
    layer = "L4"
    depends_on = ["dns"]
    what = "Opens a TCP connection to host:port."
    why = (
        "Distinguishes a blocked/closed service from a network outage. ICMP OK "
        "but TCP refused/filtered points at a firewall or a dead service."
    )
    manual_commands = ["nc -vz <host> <port>", "curl -v telnet://<host>:<port>"]

    def check(self, ctx: Context) -> CheckResult:
        targets: List[str] = ctx.get("tcp_targets", [])
        if not targets:
            return CheckResult.skip(self.name, "no TCP targets configured")

        detail = {"targets": []}
        worst = "ok"
        timeout = float(ctx.get("tcp_timeout", 3.0))
        for t in targets:
            host, _, port_s = t.rpartition(":")
            if not host:
                host, port_s = t, "80"
            try:
                port = int(port_s)
            except ValueError:
                continue
            connected, elapsed, reason = tcp_connect(host, port, timeout)
            detail["targets"].append(
                {
                    "target": f"{host}:{port}",
                    "connected": connected,
                    "ms": round(elapsed, 1),
                    "reason": reason,
                }
            )
            if not connected:
                worst = "fail" if reason != "timeout (filtered?)" else "filtered"

        ok_count = sum(1 for d in detail["targets"] if d["connected"])
        total = len(detail["targets"])
        summary = ", ".join(
            f"{d['target']} {'OK' if d['connected'] else d['reason']}"
            for d in detail["targets"]
        )
        metrics = {"connect_ok": float(ok_count), "connect_total": float(total)}
        if ok_count == total:
            return CheckResult.ok(self.name, summary, metrics=metrics, detail=detail)
        if ok_count == 0:
            return CheckResult.fail(self.name, summary, metrics=metrics, detail=detail)
        return CheckResult.warn(self.name, summary, metrics=metrics, detail=detail)
