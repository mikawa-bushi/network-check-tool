"""L3/L7 boundary: name resolution.

Deliberately separated from reachability (``external`` pings an IP). When
``external`` is OK but ``dns`` FAILs, the engine can state with confidence that
the resolver — not the network path — is the problem.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path
from typing import List, Optional

from ..core import CheckResult, Context, Probe, register
from ..core.util import have, is_linux, run


def configured_resolvers() -> List[str]:
    servers: List[str] = []
    resolv = Path("/etc/resolv.conf")
    if resolv.exists():
        try:
            for line in resolv.read_text().splitlines():
                line = line.strip()
                if line.startswith("nameserver"):
                    parts = line.split()
                    if len(parts) > 1:
                        servers.append(parts[1])
        except OSError:
            pass
    return servers


def _resolve(name: str, timeout: float = 3.0) -> Optional[List[str]]:
    socket.setdefaulttimeout(timeout)
    try:
        infos = socket.getaddrinfo(name, None)
        return sorted({i[4][0] for i in infos})
    except (socket.gaierror, socket.timeout, OSError):
        return None
    finally:
        socket.setdefaulttimeout(None)


@register
class DnsProbe(Probe):
    name = "dns"
    title = "DNS resolution"
    layer = "L7"
    depends_on = ["external"]
    what = "Reads the configured resolvers and resolves a known-good name."
    why = (
        "Isolates name-resolution faults. If reachability (external) is fine but "
        "this fails, the resolver is at fault, not connectivity."
    )
    manual_commands = [
        "cat /etc/resolv.conf",
        "getent hosts example.com",
        "nslookup example.com",
    ]

    def check(self, ctx: Context) -> CheckResult:
        name = ctx.get("dns_name", "example.com")
        resolvers = configured_resolvers()
        start = time.perf_counter()
        addrs = _resolve(name, timeout=float(ctx.get("dns_timeout", 3.0)))
        elapsed = (time.perf_counter() - start) * 1000.0

        detail = {"resolvers": resolvers, "query": name, "answers": addrs}
        if addrs is None:
            res_str = ", ".join(resolvers) if resolvers else "none configured"
            return CheckResult.fail(
                self.name,
                f"resolution of {name} failed (resolvers: {res_str})",
                metrics={"resolved": 0.0}, detail=detail,
            )
        metrics = {"resolved": 1.0, "resolve_ms": elapsed}
        if elapsed > 1000:
            return CheckResult.warn(
                self.name,
                f"{name} → {addrs[0]} but slow ({elapsed:.0f}ms)",
                metrics=metrics, detail=detail,
            )
        return CheckResult.ok(
            self.name,
            f"{name} → {', '.join(addrs[:3])} ({elapsed:.0f}ms)",
            metrics=metrics, detail=detail,
        )
