"""L3/L7 boundary: name resolution.

Deliberately separated from reachability (``external`` pings an IP). When
``external`` is OK but ``dns`` FAILs, the engine can state with confidence that
the resolver — not the network path — is the problem.

Besides the system resolver, every configured DNS server is queried directly
(stdlib UDP client), so "one of the two DNS servers is dead" is visible instead
of hiding behind a slow fallback.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import List, Optional

from ..core import CheckResult, Context, Probe, Status, register
from ..i18n import Text, tr
from ..measure.dns import DnsAnswer, DnsReplyError, query
from ._common import fmt_ms, oscmd

SLOW_MS = 1000.0
_ANSWERED = ("NOERROR", "NXDOMAIN")  # SERVFAIL, REFUSED ... mean a broken server


def configured_resolvers() -> List[str]:
    from .. import netinfo

    return list(netinfo.snapshot().resolvers)


def _resolve(name: str, timeout: float = 3.0) -> Optional[List[str]]:
    """Resolve ``name`` to a sorted list of unique addresses, or ``None``.

    Bounds the lookup with a worker thread + ``join(timeout)`` instead of
    ``socket.setdefaulttimeout`` -- the latter mutates a process-global that would
    race other probes under ``--jobs`` and clobber any timeout the host
    application had set. A lookup that overruns the budget is reported as a
    failure; its daemon thread is abandoned and exits on its own.
    """
    result: List[Optional[List[str]]] = [None]

    def _lookup() -> None:
        try:
            infos = socket.getaddrinfo(name, None)
            result[0] = sorted({i[4][0] for i in infos})
        except (socket.gaierror, OSError, UnicodeError):
            result[0] = None

    worker = threading.Thread(target=_lookup, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return None  # resolver exceeded the budget
    return result[0]


def _ask(name: str, server: str, timeout: float) -> DnsAnswer:
    """Query one server; a garbled reply makes that server bad, not the whole check."""
    try:
        return query(name, server, timeout=timeout)
    except DnsReplyError as e:
        return DnsAnswer(server=server, name=name, error=f"bad reply: {e}")


def _answers(a: DnsAnswer) -> bool:
    """The server resolved the query: an address, or a definite "no such name"."""
    return a.error is None and a.rcode in _ANSWERED


def _server_line(a: DnsAnswer) -> str:
    if a.ok:
        return f"{a.server} {fmt_ms(a.elapsed_ms)}"
    if a.error:
        return f"{a.server} " + tr(f"応答なし({a.error})", f"no answer ({a.error})")
    return f"{a.server} {a.rcode}"


@register
class DnsProbe(Probe):
    name = "dns"
    title = Text("名前解決 (DNS)", "DNS resolution")
    layer = "L7"
    depends_on = ["external"]
    requires_internet = True
    what = Text(
        "設定されたDNSサーバーで既知の名前を解決し、サーバーごとの応答も確認します。",
        "Resolves a known-good name and queries each configured resolver directly.",
    )
    why = Text(
        "名前解決の問題を切り分けます。到達性(external)がOKでこれがNGなら、原因は通信経路ではなくDNSです。",
        "Isolates name-resolution faults. If reachability (external) is fine but "
        "this fails, the resolver is at fault, not connectivity.",
    )
    manual_commands = ["nslookup example.com", "nslookup example.com <dns-server>"]

    def path_dependencies(self, ctx: Context) -> List[str]:
        """Resolvers all on this PC's LAN are asked without leaving it."""
        resolver = ctx.get("_resolver")
        servers = [resolver] if resolver else configured_resolvers()
        hn = ctx.network()
        if servers and all(hn.decide_route(s).arp for s in servers):
            return []
        return list(self.depends_on)

    def check(self, ctx: Context) -> CheckResult:
        name = ctx.get("dns_name", "example.com")
        timeout = float(ctx.get("dns_timeout", 3.0))
        resolver = ctx.get("_resolver")

        if resolver:
            # A scenario asked for a specific resolver: query exactly that server
            # and never silently fall back to the system configuration.
            a = _ask(name, resolver, timeout)
            detail = {
                "resolver": resolver,
                "query": name,
                "answers": a.addresses or None,
                "server": a.to_dict(),
            }
            if not a.ok:
                why = a.error or a.rcode or "no answer"
                return CheckResult.fail(
                    self.name,
                    tr(
                        f"{name} の名前解決に失敗（resolver {resolver}: {why}）",
                        f"resolution of {name} failed (resolver {resolver}: {why})",
                    ),
                    metrics={"resolved": 0.0},
                    detail=detail,
                    code="dns.failed",
                )
            return self._ok_or_slow(
                name, a.addresses, a.elapsed_ms or 0.0, detail, via=resolver
            )

        resolvers = configured_resolvers()
        start = time.perf_counter()
        addrs = _resolve(name, timeout=timeout)
        elapsed = (time.perf_counter() - start) * 1000.0
        per_server = [_ask(name, s, min(timeout, 2.0)) for s in resolvers[:4]]
        detail = {
            "resolvers": resolvers,
            "query": name,
            "answers": addrs,
            "servers": [a.to_dict() for a in per_server],
        }
        if addrs is None:
            target_desc = (
                tr("DNSサーバー: ", "resolvers: ")
                + ", ".join(_server_line(a) for a in per_server)
                if per_server
                else tr("DNSサーバー未設定", "resolvers: none configured")
            )
            return CheckResult.fail(
                self.name,
                tr(
                    f"{name} の名前解決に失敗（{target_desc}）",
                    f"resolution of {name} failed ({target_desc})",
                ),
                metrics={"resolved": 0.0},
                detail=detail,
                code="dns.failed" if resolvers else "dns.no_resolver",
            )
        lines = ", ".join(_server_line(a) for a in per_server)
        dead = [a for a in per_server if not _answers(a)]
        if dead and len(dead) == len(per_server):
            return CheckResult.fail(
                self.name,
                tr(
                    f"{name} はキャッシュ/hostsからは解決できますが、DNSサーバーが1台も応答しません: {lines}",
                    f"{name} resolves only from the cache or hosts file; no DNS server answers: {lines}",
                ),
                metrics={"resolved": 1.0, "resolve_ms": elapsed},
                detail=detail,
                code="dns.all_servers_down",
            )
        if dead:
            return CheckResult.warn(
                self.name,
                tr(
                    f"{name} は解決できますが応答しないDNSサーバーがあります: {lines}",
                    f"{name} resolves but a DNS server is not answering: {lines}",
                ),
                metrics={"resolved": 1.0, "resolve_ms": elapsed},
                detail=detail,
                code="dns.server_down",
            )
        return self._ok_or_slow(name, addrs, elapsed, detail)

    def _ok_or_slow(
        self, name, addrs, elapsed, detail, via: Optional[str] = None
    ) -> CheckResult:
        metrics = {"resolved": 1.0, "resolve_ms": elapsed}
        suffix = f" via {via}" if via else ""
        if elapsed > SLOW_MS:
            return CheckResult.warn(
                self.name,
                tr(
                    f"{name} → {addrs[0]} ですが遅い ({elapsed:.0f}ms){suffix}",
                    f"{name} → {addrs[0]} but slow ({elapsed:.0f}ms){suffix}",
                ),
                metrics=metrics,
                detail=detail,
                code="dns.slow",
            )
        return CheckResult.ok(
            self.name,
            f"{name} → {', '.join(addrs[:3])} ({elapsed:.0f}ms){suffix}",
            metrics=metrics,
            detail=detail,
            code="dns.ok",
        )

    def advice(self, result, results=None):
        code = result.code if result is not None else ""
        detail = result.detail if result is not None else {}
        name = detail.get("query") or "example.com"
        servers = list(detail.get("resolvers") or [])
        if detail.get("resolver"):
            servers = [detail["resolver"]]
        bad = [
            s["server"]
            for s in detail.get("servers") or []
            if s.get("error") or s.get("rcode") not in _ANSWERED
        ]
        server = (bad or servers or [tr("<DNSサーバー>", "<resolver-ip>")])[0]
        external = (results or {}).get("external")
        reachable = external is not None and external.status == Status.OK
        if code == "dns.no_resolver":
            means = tr(
                "DNSサーバーが設定されていないため、名前を解決できません。",
                "No DNS server is configured, so names cannot be resolved.",
            )
        elif code == "dns.server_down":
            means = tr(
                f"名前は解決できますが、DNSサーバー {', '.join(bad)} が正しく応答しません。そのサーバーに当たると名前解決が遅れたり失敗したりします。",
                f"Names resolve, but the DNS server {', '.join(bad)} does not answer "
                f"properly; lookups that hit it are slow or fail.",
            )
        elif code == "dns.all_servers_down":
            means = tr(
                "DNSサーバーが1台も応答しません。キャッシュやhostsにある名前しか解決できません。",
                "No DNS server answers; only names in the cache or hosts file resolve.",
            )
        elif code == "dns.slow":
            means = tr(
                "名前解決はできますが遅く、接続のたびに待たされます。",
                "Names resolve, but slowly, which delays every new connection.",
            )
        elif reachable:
            means = tr(
                "通信はできていますが名前解決ができません — DNSサーバーの問題です。",
                "Connectivity is fine but names don't resolve — a DNS/resolver fault.",
            )
        else:
            means = tr(
                "名前解決ができません — DNSサーバーかその経路の問題です。",
                "Names don't resolve — a fault in the DNS server or the path to it.",
            )
        return (
            means,
            [
                tr(
                    f"DNSサーバーに直接問い合わせる: nslookup {name} {server}",
                    f"Test the resolver directly: nslookup {name} {server}",
                ),
                tr(
                    "設備ネットワークではIPアドレスで直接指定すると確実です",
                    "On equipment networks, addressing devices by IP avoids DNS entirely.",
                ),
                tr(
                    f"DNS設定を確認: {oscmd('resolvectl status / cat /etc/resolv.conf', 'scutil --dns')}",
                    f"Inspect the DNS settings: {oscmd('resolvectl status / cat /etc/resolv.conf', 'scutil --dns')}",
                ),
            ],
        )
