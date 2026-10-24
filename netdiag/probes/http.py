"""L7: HTTP reachability with per-phase timing.

'The web is slow' decomposes into DNS / TCP connect / TLS handshake / first
byte. Splitting the time tells you whether the network or the server is slow.
Implemented with stdlib only (socket + ssl + http.client).
"""

from __future__ import annotations

import http.client
import socket
import ssl
import time
from typing import Dict, Optional
from urllib.parse import urlparse

from ..core import CheckResult, Context, Probe, register
from ..i18n import Text, tr
from .dns import _resolve


def _timed_get(url: str, timeout: float = 5.0) -> Dict:
    parsed = urlparse(url if "://" in url else "http://" + url)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or ""
    port = parsed.port or (443 if scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    phases: Dict[str, Optional[float]] = {
        "dns_ms": None,
        "connect_ms": None,
        "tls_ms": None,
        "ttfb_ms": None,
    }
    info: Dict = {"url": url, "host": host, "port": port, "scheme": scheme}

    # DNS
    t0 = time.perf_counter()
    addrs = _resolve(host, timeout=timeout)
    phases["dns_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    if not addrs:
        info["error"] = "dns resolution failed"
        info["phases"] = phases
        return info
    ip = addrs[0]

    # TCP connect
    t0 = time.perf_counter()
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
    except OSError as e:
        info["error"] = f"connect failed: {e}"
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
            path,
            headers={"Host": host, "User-Agent": "netdiag", "Connection": "close"},
        )
        resp = conn.getresponse()
        phases["ttfb_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        info["status_code"] = resp.status
        resp.read(1)
        conn.close()
    except (ssl.SSLError, OSError, http.client.HTTPException) as e:
        info["error"] = f"http error: {e}"
    info["phases"] = phases
    return info


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

    def check(self, ctx: Context) -> CheckResult:
        url = ctx.get("http_url")
        if not url:
            return CheckResult.skip(
                self.name,
                tr("URLが指定されていません", "no http_url configured"),
                code="http.none",
            )
        info = _timed_get(url, timeout=float(ctx.get("http_timeout", 5.0)))
        phases = info.get("phases", {})
        metrics = {k: v for k, v in phases.items() if v is not None}
        if "status_code" in info:
            metrics["status_code"] = float(info["status_code"])

        if "error" in info:
            return CheckResult.fail(
                self.name,
                f"{url}: {info['error']}",
                metrics=metrics,
                detail=info,
                code="http.failed",
            )
        code = info.get("status_code", 0)
        phase_str = " ".join(
            f"{k.replace('_ms','')}={v}ms" for k, v in phases.items() if v is not None
        )
        if code >= 500:
            return CheckResult.fail(
                self.name,
                tr(
                    f"{url}: HTTP {code}（サーバーエラー） [{phase_str}]",
                    f"{url}: HTTP {code} (server error) [{phase_str}]",
                ),
                metrics=metrics,
                detail=info,
                code="http.server_error",
            )
        if code >= 400:
            return CheckResult.warn(
                self.name,
                f"{url}: HTTP {code} [{phase_str}]",
                metrics=metrics,
                detail=info,
                code="http.client_error",
            )
        return CheckResult.ok(
            self.name,
            f"{url}: HTTP {code} [{phase_str}]",
            metrics=metrics,
            detail=info,
            code="http.ok",
        )
