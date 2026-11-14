"""L7: HTTP reachability with per-phase timing.

'The web is slow' decomposes into DNS / TCP connect / TLS handshake / first
byte. Splitting the time tells you whether the network or the server is slow.
Implemented with stdlib only (socket + ssl + http.client).
"""

from __future__ import annotations

import http.client
import socket
import ssl
import threading
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlparse

from ..core import CheckResult, Context, InputError, Probe, register
from ..i18n import Text, tr
from ..measure.icmp import _literal_family
from .tcp import parse_tcp_targets

MAX_REDIRECTS = 5
_REDIRECTS = (301, 302, 303, 307, 308)


def _addresses(host: str, port: int, timeout: float) -> Optional[List[tuple]]:
    """``getaddrinfo`` results in the OS's preferred order, bounded by *timeout*."""
    result: List[Optional[List[tuple]]] = [None]

    def _lookup() -> None:
        try:
            result[0] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            result[0] = None

    worker = threading.Thread(target=_lookup, daemon=True)
    worker.start()
    worker.join(timeout)
    return None if worker.is_alive() else result[0]


def _connect(infos: List[tuple], timeout: float) -> Tuple[socket.socket, str]:
    """Connect to the first address that accepts, in order, as browsers do."""
    errors: List[str] = []
    for family, kind, proto, _name, sockaddr in infos:
        sock = socket.socket(family, kind, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(sockaddr)
            return sock, sockaddr[0]
        except OSError as e:
            sock.close()
            errors.append(f"{sockaddr[0]}: {e}")
    raise OSError("; ".join(errors))


def _request_target(parsed) -> str:
    """Path and query as sent on the wire: non-ASCII text percent-encoded."""
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if parsed.query:
        path += "?" + quote(parsed.query, safe="/%:@!$&'()*+,;=-._~?")
    return path


def _timed_get(url: str, timeout: float = 5.0) -> Dict:
    parsed = urlparse(url if "://" in url else "http://" + url)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or ""
    port = parsed.port or (443 if scheme == "https" else 80)

    phases: Dict[str, Optional[float]] = {
        "dns_ms": None,
        "connect_ms": None,
        "tls_ms": None,
        "ttfb_ms": None,
    }
    info: Dict = {"url": url, "host": host, "port": port, "scheme": scheme}

    # DNS
    t0 = time.perf_counter()
    infos = _addresses(host, port, timeout)
    phases["dns_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    if not infos:
        info["error"] = "dns resolution failed"
        info["stage"] = "dns"
        info["phases"] = phases
        return info

    # TCP connect
    t0 = time.perf_counter()
    try:
        sock, info["address"] = _connect(infos, timeout)
    except OSError as e:
        info["error"] = f"connect failed: {e}"
        info["stage"] = "connect"
        info["phases"] = phases
        return info
    phases["connect_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    # TLS
    try:
        if scheme == "https":
            t0 = time.perf_counter()
            ctx_ssl = ssl.create_default_context()
            sock = ctx_ssl.wrap_socket(sock, server_hostname=host)
            phases["tls_ms"] = round((time.perf_counter() - t0) * 1000, 1)

        # Request + first byte
        t0 = time.perf_counter()
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.sock = sock
        conn.request(
            "GET",
            _request_target(parsed),
            headers={
                "Host": parsed.netloc.rsplit("@", 1)[-1].encode("idna").decode(),
                "User-Agent": "netdiag",
                "Connection": "close",
            },
        )
        resp = conn.getresponse()
        phases["ttfb_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        info["status_code"] = resp.status
        info["location"] = resp.getheader("Location")
        resp.read(1)
        conn.close()
    except (ssl.SSLError, OSError, http.client.HTTPException, UnicodeError) as e:
        info["error"] = f"http error: {e}"
        info["stage"] = "http"
    info["phases"] = phases
    return info


def _get_following_redirects(url: str, timeout: float) -> Tuple[Dict, List[str]]:
    """The final response and the chain of URLs that led to it."""
    chain = [url]
    info = _timed_get(url, timeout=timeout)
    while (
        info.get("status_code") in _REDIRECTS
        and info.get("location")
        and len(chain) <= MAX_REDIRECTS
    ):
        url = urljoin(chain[-1], info["location"])
        chain.append(url)
        info = _timed_get(url, timeout=timeout)
    return info, chain


def _same_endpoint(url: str, pairs: List[Tuple[str, int]], default_port: int) -> bool:
    parsed = urlparse(url if "://" in url else "http://" + url)
    port = parsed.port or default_port
    return any(h == parsed.hostname and p == port for h, p in pairs)


def endpoint_dependencies(
    probe: Probe, ctx: Context, url: Optional[str], default_port: int
) -> List[str]:
    """Drop ``dns`` for an IP address and ``tcp`` unless it tests this endpoint."""
    deps = list(probe.depends_on)
    if not url:
        return deps
    host = urlparse(url if "://" in url else "http://" + url).hostname or ""
    if _literal_family(host):
        deps = [d for d in deps if d != "dns"]
    try:
        pairs = parse_tcp_targets(ctx.get("tcp_targets") or [])
    except InputError:
        pairs = []
    if not _same_endpoint(url, pairs, default_port):
        deps = [d for d in deps if d != "tcp"]
    return deps


@register
class HttpProbe(Probe):
    name = "http"
    title = Text("HTTPサービス", "HTTP service")
    layer = "L7"
    depends_on = ["dns", "tcp"]
    what = Text(
        "URLを取得し、DNS・TCP接続・TLS・最初の応答までの時間を分けて測ります。",
        "Fetches a URL and times DNS, TCP, TLS and first-byte separately.",
    )
    why = Text(
        "『Webが遅い/つながらない』を段階ごとに分解し、ネットワークの問題かサーバーの問題かを切り分けます。",
        "Turns 'the site is slow/down' into a phase breakdown so you can tell a "
        "network problem from a slow or erroring server.",
    )
    means = Text(
        "サーバーまでは届いていますが、HTTPリクエストが失敗またはエラー(5xx)になりました。",
        "Network reaches the server but the HTTP request failed or 5xx'd.",
    )
    runbook = [
        Text(
            "段階別の時間を見る: connect が速く ttfb が遅いならサーバー側の処理が遅い",
            "Read the phase timing: a slow `ttfb` with fast `connect` is a slow server.",
        ),
        Text(
            "サーバーのログ/ヘルスチェックを確認する",
            "Check the server's own logs / health endpoint.",
        ),
    ]
    manual_commands = [
        "curl -o /dev/null -s -w "
        "'dns:%{time_namelookup} conn:%{time_connect} tls:%{time_appconnect} "
        "ttfb:%{time_starttransfer}\\n' <url>",
    ]

    def path_dependencies(self, ctx: Context) -> List[str]:
        url = ctx.get("http_url")
        default_port = 443 if str(url or "").startswith("https://") else 80
        return endpoint_dependencies(self, ctx, url, default_port)

    def advice(self, result, results=None):
        means, steps = super().advice(result, results)
        code = result.code if result is not None else ""
        if code == "http.dns_failed":
            means = tr(
                "URLのサーバー名をIPアドレスに変換できません。",
                "The server name in the URL does not resolve.",
            )
        elif code == "http.connect_failed":
            means = tr(
                "サーバーがTCP接続を受け付けません（停止中か、途中で遮断されています）。",
                "The server does not accept the TCP connection (down, or blocked "
                "on the way).",
            )
        return means, steps

    def check(self, ctx: Context) -> CheckResult:
        url = ctx.get("http_url")
        if not url:
            return CheckResult.skip(
                self.name,
                tr("URLが指定されていません", "no http_url configured"),
                code="http.none",
            )
        info, chain = _get_following_redirects(
            url, timeout=float(ctx.get("http_timeout", 5.0))
        )
        info["redirects"] = chain[1:]
        phases = info.get("phases", {})
        metrics = {k: v for k, v in phases.items() if v is not None}
        if "status_code" in info:
            metrics["status_code"] = float(info["status_code"])
        shown = " → ".join(chain)

        if "error" in info:
            code = {"dns": "http.dns_failed", "connect": "http.connect_failed"}.get(
                info.get("stage", ""), "http.failed"
            )
            return CheckResult.fail(
                self.name,
                f"{shown}: {info['error']}",
                metrics=metrics,
                detail=info,
                code=code,
            )
        code = info.get("status_code", 0)
        phase_str = " ".join(
            f"{k.replace('_ms','')}={v}ms" for k, v in phases.items() if v is not None
        )
        if code >= 500:
            return CheckResult.fail(
                self.name,
                tr(
                    f"{shown}: HTTP {code}（サーバーエラー） [{phase_str}]",
                    f"{shown}: HTTP {code} (server error) [{phase_str}]",
                ),
                metrics=metrics,
                detail=info,
                code="http.server_error",
            )
        if code >= 300:
            return CheckResult.warn(
                self.name,
                f"{shown}: HTTP {code} [{phase_str}]",
                metrics=metrics,
                detail=info,
                code="http.client_error" if code >= 400 else "http.redirect",
            )
        return CheckResult.ok(
            self.name,
            f"{shown}: HTTP {code} [{phase_str}]",
            metrics=metrics,
            detail=info,
            code="http.ok",
        )
