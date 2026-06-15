"""L3/L7 boundary: name resolution.

Deliberately separated from reachability (``external`` pings an IP). When
``external`` is OK but ``dns`` FAILs, the engine can state with confidence that
the resolver — not the network path — is the problem.
"""

from __future__ import annotations

import re
import socket
import time
from pathlib import Path
from typing import List, Optional

from ..core import CheckResult, Context, Probe, register
from ..core.util import have, is_linux, run

_NSLOOKUP_ADDR_RE = re.compile(r"^Address:\s*(\S+)", re.MULTILINE)


def _parse_nslookup_addresses(text: str) -> List[str]:
    """Extract the answer addresses from ``nslookup`` output.

    Answer lines look like ``Address: 93.184.216.34``; the queried server's own
    line looks like ``Address: 8.8.8.8#53`` (with a ``#port`` suffix). We keep the
    values without a ``#`` — that drops the server line and keeps A/AAAA answers.
    Returns sorted unique addresses.
    """
    addrs = [m for m in _NSLOOKUP_ADDR_RE.findall(text) if "#" not in m]
    return sorted(set(addrs))


def _resolve_via_server(
    name: str, server: str, timeout: float = 3.0
) -> Optional[List[str]]:
    """Resolve ``name`` against a specific DNS ``server`` via ``nslookup``.

    Returns the answer addresses, or ``None`` if the query produced none.
    """
    res = run(["nslookup", name, server], timeout=timeout)
    addrs = _parse_nslookup_addresses(res.stdout or res.stderr or "")
    return addrs or None


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
        timeout = float(ctx.get("dns_timeout", 3.0))
        resolver = ctx.get("_resolver")

        if resolver:
            # A scenario asked for a specific resolver. Honor it via nslookup and
            # never silently fall back to the system resolver (the M5 defect).
            if not have("nslookup"):
                return CheckResult.warn(
                    self.name,
                    f"custom resolver {resolver} requested but 'nslookup' is "
                    f"unavailable to query it",
                    metrics={"resolved": 0.0},
                    detail={"resolver": resolver, "query": name},
                )
            start = time.perf_counter()
            addrs = _resolve_via_server(name, resolver, timeout=timeout)
            elapsed = (time.perf_counter() - start) * 1000.0
            detail = {"resolver": resolver, "query": name, "answers": addrs}
            target_desc = f"resolver {resolver}"
        else:
            resolvers = configured_resolvers()
            start = time.perf_counter()
            addrs = _resolve(name, timeout=timeout)
            elapsed = (time.perf_counter() - start) * 1000.0
            detail = {"resolvers": resolvers, "query": name, "answers": addrs}
            target_desc = (
                f"resolvers: {', '.join(resolvers) if resolvers else 'none configured'}"
            )

        if addrs is None:
            return CheckResult.fail(
                self.name,
                f"resolution of {name} failed ({target_desc})",
                metrics={"resolved": 0.0},
                detail=detail,
            )
        metrics = {"resolved": 1.0, "resolve_ms": elapsed}
        suffix = f" via {resolver}" if resolver else ""
        if elapsed > 1000:
            return CheckResult.warn(
                self.name,
                f"{name} → {addrs[0]} but slow ({elapsed:.0f}ms){suffix}",
                metrics=metrics,
                detail=detail,
            )
        return CheckResult.ok(
            self.name,
            f"{name} → {', '.join(addrs[:3])} ({elapsed:.0f}ms){suffix}",
            metrics=metrics,
            detail=detail,
        )
