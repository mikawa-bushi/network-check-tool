"""L7: TLS certificate validity, expiry and clock-skew failures.

A surprising share of 'site won't load' cases are expired certs or a wrong local
clock making valid certs look not-yet-valid. Checking this explicitly keeps the
diagnosis from blaming the network.
"""

from __future__ import annotations

import socket
import ssl
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from ..core import CheckResult, Context, Probe, register
from ..i18n import Text, tr


def _parse_cert_time(s: str) -> Optional[datetime]:
    # e.g. 'Jun  1 12:00:00 2026 GMT'
    try:
        return datetime.strptime(s, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@register
class TlsProbe(Probe):
    name = "tls"
    title = Text("TLS証明書", "TLS certificate")
    layer = "L7"
    depends_on = ["tcp"]
    what = Text(
        "相手の証明書を取得し、有効期間とホスト名を確認します。",
        "Fetches the peer certificate and checks validity window and hostname.",
    )
    why = Text(
        "証明書の期限切れやPCの時計ずれは、ネットワーク障害のように見えます。",
        "Expired/not-yet-valid certs and local clock skew masquerade as network "
        "failures; verifying them here removes that ambiguity.",
    )
    means = Text(
        "TLSハンドシェイクまたは証明書の検証に失敗しました。",
        "The TLS handshake or certificate validation failed.",
    )
    runbook = [
        Text(
            "証明書の有効期限と、このPCの時計（時刻ずれはTLSを壊します）を確認する",
            "Check the certificate expiry and your system clock (skew breaks TLS).",
        ),
        Text(
            "openssl s_client -connect <host>:443 で証明書チェーンを確認する",
            "`openssl s_client -connect <host>:443` to see the chain.",
        ),
    ]
    manual_commands = [
        "echo | openssl s_client -connect <host>:443 -servername <host> 2>/dev/null "
        "| openssl x509 -noout -dates",
    ]

    def check(self, ctx: Context) -> CheckResult:
        target = ctx.get("tls_target") or ctx.get("http_url")
        if not target:
            return CheckResult.skip(
                self.name,
                tr("対象が指定されていません", "no tls_target configured"),
                code="tls.none",
            )
        parsed = urlparse(target if "://" in target else "https://" + target)
        host = parsed.hostname or target
        port = parsed.port or 443
        timeout = float(ctx.get("tls_timeout", 5.0))

        ssl_ctx = ssl.create_default_context()
        try:
            with socket.create_connection((host, port), timeout=timeout) as sock:
                with ssl_ctx.wrap_socket(sock, server_hostname=host) as ssock:
                    cert = ssock.getpeercert()
        except ssl.SSLCertVerificationError as e:
            return CheckResult.fail(
                self.name,
                tr(
                    f"{host}:{port} 証明書の検証に失敗: {e.verify_message}",
                    f"{host}:{port} certificate verification failed: {e.verify_message}",
                ),
                detail={"host": host, "error": str(e)},
                code="tls.verify_failed",
            )
        except (ssl.SSLError, OSError) as e:
            return CheckResult.fail(
                self.name,
                tr(
                    f"{host}:{port} TLSハンドシェイク失敗: {e}",
                    f"{host}:{port} TLS handshake failed: {e}",
                ),
                detail={"host": host, "error": str(e)},
                code="tls.handshake_failed",
            )

        not_after = _parse_cert_time(cert.get("notAfter", "")) if cert else None
        not_before = _parse_cert_time(cert.get("notBefore", "")) if cert else None
        now = datetime.now(timezone.utc)
        detail = {"host": host, "cert": cert}

        if not_before and now < not_before:
            return CheckResult.fail(
                self.name,
                tr(
                    f"{host}: 証明書は {not_before:%Y-%m-%d} まで有効になりません（PCの時計がずれている?）",
                    f"{host}: certificate not valid until {not_before:%Y-%m-%d} (local clock wrong?)",
                ),
                detail=detail,
                code="tls.not_yet_valid",
            )
        if not_after:
            days = (not_after - now).total_seconds() / 86400.0
            detail["days_to_expiry"] = round(days, 1)
            if days < 0:
                return CheckResult.fail(
                    self.name,
                    tr(
                        f"{host}: 証明書は {abs(days):.0f} 日前に期限切れ",
                        f"{host}: certificate EXPIRED {abs(days):.0f} days ago",
                    ),
                    metrics={"days_to_expiry": days},
                    detail=detail,
                    code="tls.expired",
                )
            if days < 14:
                return CheckResult.warn(
                    self.name,
                    tr(
                        f"{host}: 証明書の期限まで残り {days:.0f} 日",
                        f"{host}: certificate expires in {days:.0f} days",
                    ),
                    metrics={"days_to_expiry": days},
                    detail=detail,
                    code="tls.expiring",
                )
            return CheckResult.ok(
                self.name,
                tr(
                    f"{host}: 証明書は有効（残り {days:.0f} 日）",
                    f"{host}: certificate valid ({days:.0f} days left)",
                ),
                metrics={"days_to_expiry": days},
                detail=detail,
                code="tls.ok",
            )
        return CheckResult.ok(
            self.name,
            tr(f"{host}: TLS接続OK", f"{host}: TLS handshake OK"),
            detail=detail,
            code="tls.ok",
        )
