"""L7: TLS certificate validity, expiry and clock-skew failures.

A surprising share of 'site won't load' cases are expired certs or a wrong local
clock making valid certs look not-yet-valid. Checking this explicitly keeps the
diagnosis from blaming the network.
"""

from __future__ import annotations

import socket
import ssl
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from ..core import CheckResult, Context, Probe, register
from ..i18n import Text, tr
from .http import endpoint_dependencies

# OpenSSL verify codes for a certificate outside its validity window.
_NOT_YET_VALID = 9
_EXPIRED = 10


def _parse_cert_time(s: str) -> Optional[datetime]:
    # e.g. 'Jun  1 12:00:00 2026 GMT'
    try:
        return datetime.strptime(s, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _tlv(der: bytes, i: int) -> Tuple[int, int, int]:
    """One DER element at *i*: (tag, start of value, end of value)."""
    tag, n = der[i], der[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(der[i : i + k], "big")
        i += k
    return tag, i, i + n


def _asn1_time(der: bytes, start: int, end: int) -> datetime:
    text = der[start:end].decode("ascii").rstrip("Z")
    fmt = "%y%m%d%H%M%S" if len(text) == 12 else "%Y%m%d%H%M%S"
    return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)


def cert_validity(der: bytes) -> Tuple[datetime, datetime]:
    """(notBefore, notAfter) of a DER certificate. Raises ValueError/IndexError."""
    _t, tbs, _e = _tlv(der, 0)  # Certificate
    _t, pos, _e = _tlv(der, tbs)  # TBSCertificate
    tag, _s, end = _tlv(der, pos)
    if tag == 0xA0:  # explicit version
        pos = end
    for _field in ("serial", "signature", "issuer"):
        pos = _tlv(der, pos)[2]
    _t, pos, _e = _tlv(der, pos)  # Validity
    _t, s1, e1 = _tlv(der, pos)
    _t, s2, e2 = _tlv(der, e1)
    return _asn1_time(der, s1, e1), _asn1_time(der, s2, e2)


def _unverified_cert(host: str, port: int, timeout: float) -> Optional[bytes]:
    """The server's certificate without verifying it (to read its dates)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                return ssock.getpeercert(binary_form=True)
    except (ssl.SSLError, OSError):
        return None


def tls_endpoint(ctx: Context) -> Optional[str]:
    """What to check: ``tls_target``, else an ``https://`` ``http_url``.

    A plain ``http://`` URL has no TLS to check.
    """
    target = ctx.get("tls_target")
    if not target:
        url = str(ctx.get("http_url") or "")
        target = url if url.lower().startswith("https://") else None
    if target and urlparse(target).scheme.lower() == "http":
        return None
    return target


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

    def path_dependencies(self, ctx: Context) -> List[str]:
        target = tls_endpoint(ctx)
        url = target if target and "://" in target else f"https://{target}"
        return endpoint_dependencies(self, ctx, url if target else None, 443)

    def check(self, ctx: Context) -> CheckResult:
        target = tls_endpoint(ctx)
        if not target:
            return CheckResult.skip(
                self.name,
                tr("TLSの確認対象（https）がありません", "no https target to check"),
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
            if e.verify_code in (_NOT_YET_VALID, _EXPIRED):
                return self._outside_validity(host, port, timeout, e)
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
        now = datetime.now(timezone.utc)
        detail = {"host": host, "cert": cert}

        if not_after:
            days = (not_after - now).total_seconds() / 86400.0
            detail["days_to_expiry"] = round(days, 1)
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

    def _outside_validity(
        self, host: str, port: int, timeout: float, error: ssl.SSLCertVerificationError
    ) -> CheckResult:
        """Expired or not yet valid: the dates say whether to blame the clock.

        Verification rejects such a certificate before its dates can be read,
        so they come from a second, unverified handshake.
        """
        now = datetime.now(timezone.utc)
        detail = {"host": host, "error": str(error), "now": now.isoformat()}
        try:
            der = _unverified_cert(host, port, timeout)
            not_before, not_after = cert_validity(der) if der else (None, None)
        except (ValueError, IndexError):
            not_before, not_after = None, None
        if error.verify_code == _NOT_YET_VALID:
            since = f" {not_before:%Y-%m-%d}" if not_before else ""
            detail["not_before"] = not_before.isoformat() if not_before else None
            return CheckResult.fail(
                self.name,
                tr(
                    f"{host}: 証明書はまだ有効になっていません（有効開始{since}、このPCの時刻 {now:%Y-%m-%d}）— PCの時計がずれていませんか?",
                    f"{host}: certificate not valid until{since or ' later'} "
                    f"(this PC's clock says {now:%Y-%m-%d}) — local clock wrong?",
                ),
                detail=detail,
                code="tls.not_yet_valid",
            )
        days = (now - not_after).total_seconds() / 86400.0 if not_after else None
        detail["days_to_expiry"] = -round(days, 1) if days is not None else None
        ago = tr(f" {days:.0f} 日前に", f" {days:.0f} days ago") if days else ""
        return CheckResult.fail(
            self.name,
            tr(
                f"{host}: 証明書は{ago}期限切れです（このPCの時刻 {now:%Y-%m-%d}）",
                f"{host}: certificate EXPIRED{ago} (this PC's clock says {now:%Y-%m-%d})",
            ),
            metrics={"days_to_expiry": -days} if days is not None else {},
            detail=detail,
            code="tls.expired",
        )
