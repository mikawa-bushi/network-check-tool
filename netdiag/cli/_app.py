"""The ``netdiag`` click group and helpers shared by every subcommand."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

import click

from .. import __version__
from ..i18n import detect, set_language, tr

logger = logging.getLogger("netdiag.cli")

# Help texts are chosen once, from the environment (NETDIAG_LANG / locale),
# because click renders them before any option is parsed.
_HELP_JA = detect() == "ja"


def H(ja: str, en: str) -> str:
    """Help text in the environment's language."""
    return ja if _HELP_JA else en


def _setup_logging(verbose: bool = False, log_file: Optional[str] = None) -> None:
    """Configure the ``netdiag`` logger for CLI use (library code never does this).

    Quiet by default: with neither ``verbose`` nor ``log_file`` set, no handler is
    attached and the package's NullHandler keeps things silent. ``verbose`` lowers
    the level to DEBUG and logs to stderr; ``log_file`` writes to that file instead.
    """
    if not verbose and not log_file:
        return
    lg = logging.getLogger("netdiag")
    # Idempotent: if a real (non-Null) handler is already attached, don't stack
    # another one (avoids duplicate lines / leaked file handles on repeat calls).
    if any(not isinstance(h, logging.NullHandler) for h in lg.handlers):
        return
    lg.setLevel(logging.DEBUG if verbose else logging.WARNING)
    handler: logging.Handler = (
        logging.FileHandler(log_file, encoding="utf-8")
        if log_file
        else logging.StreamHandler()
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    lg.addHandler(handler)


def _resolve_version() -> str:
    """Installed package version, falling back to the in-tree __version__.

    Lets ``netdiag --version`` work both when pip-installed and when run from a
    source checkout (where the distribution metadata isn't present).
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("network-check-tool")
    except PackageNotFoundError:
        return __version__


DEFAULT_CONFIG = {
    "external_ip": "1.1.1.1",
    "dns_name": "example.com",
}


class HttpUrl(click.ParamType):
    """An http:// or https:// URL. A bare ``host[:port][/path]`` means http://."""

    name = "URL"

    def convert(self, value, param, ctx) -> str:
        url = value if "://" in value else "http://" + value
        try:
            parsed = urlparse(url)
            usable = (
                parsed.scheme.lower() in ("http", "https")
                and bool(parsed.hostname)
                and parsed.port != 0  # .port raises ValueError when out of range
            )
        except ValueError:
            usable = False
        if not usable:
            self.fail(
                tr(
                    f"http:// か https:// で始まる URL を指定してください: {value}",
                    f"give an http:// or https:// URL: {value}",
                ),
                param,
                ctx,
            )
        return url


def _build_config(external_ip, dns_name, tcp, http_url, count, internet=False) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    # Naming an external target or DNS name means "I expect internet access".
    cfg["internet"] = bool(internet or external_ip or dns_name)
    if external_ip:
        cfg["external_ip"] = external_ip
    if dns_name:
        cfg["dns_name"] = dns_name
    if tcp:
        cfg["tcp_targets"] = list(tcp)
    if http_url:
        cfg["http_url"] = http_url
        if urlparse(http_url).scheme.lower() == "https":
            cfg["tls_target"] = http_url
    if count:
        cfg["count"] = count
    return cfg


def _split_csv(value: Optional[str]) -> Optional[List[str]]:
    """Parse a comma-separated CLI option into a stripped list, or None if unset."""
    return [s.strip() for s in value.split(",") if s.strip()] if value else None


def write_output(text: str, output: Optional[str], fmt: str) -> None:
    """Print *text*, or save it (CSV with a BOM so Excel reads Japanese)."""
    if output:
        encoding = "utf-8-sig" if fmt == "csv" else "utf-8"
        Path(output).write_text(
            text, encoding=encoding, newline="" if fmt == "csv" else None
        )
        click.echo(
            H(f"レポートを保存しました: {output}", f"report written to {output}"),
            err=True,
        )
    else:
        click.echo(text)


def format_from_path(
    output: Optional[str], fmt: Optional[str], default: str = "text"
) -> str:
    """``-o report.html`` implies ``--format html`` unless one was given."""
    if fmt:
        return fmt
    if output:
        suffix = Path(output).suffix.lower().lstrip(".")
        if suffix in ("html", "htm"):
            return "html"
        if suffix in ("json", "csv"):
            return suffix
    return default


def color_wanted(no_color: bool) -> bool:
    return sys.stdout.isatty() and not no_color


# Typographic characters netdiag prints, with stand-ins for encodings that
# lack them (redirected output on Windows uses the ANSI code page, e.g. cp932,
# which has no "—", "≤" or "✔"). The first candidate the encoding can
# represent wins; anything else becomes "?". Never raise on output.
_SUBSTITUTES = {
    "—": ("―", "-"),
    "–": ("-",),
    "·": ("・", "."),
    "≤": ("≦", "<="),
    "≥": ("≧", ">="),
    "⇄": ("<->",),
    "✔": ("○", "OK"),
    "✘": ("×", "NG"),
    "⚠": ("!",),
    "━": ("─", "="),
    "■": ("#",),
    "…": ("...",),
    "〜": ("～", "~"),
    "・": (".",),
    "（": ("(",),
    "）": (")",),
    "\ufeff": ("",),
}


def _substitute(exc: UnicodeError):
    if not isinstance(exc, UnicodeEncodeError):
        raise exc
    out = []
    for ch in exc.object[exc.start : exc.end]:
        for cand in _SUBSTITUTES.get(ch, ()):
            try:
                cand.encode(exc.encoding)
            except (UnicodeEncodeError, LookupError):
                continue
            out.append(cand)
            break
        else:
            out.append("?")
    return "".join(out), exc.end


def harden_streams() -> None:
    """Make stdout/stderr substitute instead of crash on unencodable text."""
    import codecs

    try:
        codecs.lookup_error("netdiag")
    except LookupError:
        codecs.register_error("netdiag", _substitute)
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        reconfigure = getattr(stream, "reconfigure", None)
        if enc and enc not in ("utf8", "utf8sig") and reconfigure is not None:
            try:
                reconfigure(errors="netdiag")
            except (ValueError, OSError):
                pass


def setup(
    lang: Optional[str] = None, verbose: bool = False, log_file: Optional[str] = None
) -> None:
    """Per-run setup shared by the ``netdiag`` group and the exe launcher."""
    from .. import probes  # noqa: F401  (registers the built-in probes)
    from ..plugins import load_plugins

    set_language(lang)
    _setup_logging(verbose=verbose, log_file=log_file)
    load_plugins()


def _next_steps() -> str:
    from ..report.common import pad, width

    steps = [
        (
            tr("台帳の機器もまとめて診断", "check every device in a list"),
            tr("netdiag check -f 台帳.csv", "netdiag check -f devices.csv"),
        ),
        (
            tr("同じネットワークの機器を探す", "find devices on this network"),
            "netdiag scan",
        ),
        (
            tr("ときどき切れる原因を探す", "catch intermittent drops"),
            "netdiag soak 192.168.3.39 --duration 1h",
        ),
        (tr("使えるコマンドの一覧", "list every command"), "netdiag --help"),
    ]
    cols = max(width(what) for what, _ in steps)
    lines = ["", tr("次にできること:", "What next:")]
    lines += [f"  {pad(what, cols)}  {cmd}" for what, cmd in steps]
    return "\n".join(lines)


class Progress:
    """One overwriting status line on stderr (only on an interactive terminal)."""

    def __init__(self, enabled: Optional[bool] = None) -> None:
        self.enabled = sys.stderr.isatty() if enabled is None else enabled
        self._shown = False

    def __call__(self, message: str) -> None:
        if self.enabled:
            click.echo(f"\r\033[K… {message}", nl=False, err=True)
            self._shown = True

    def done(self) -> None:
        if self.enabled and self._shown:
            click.echo("\r\033[K", nl=False, err=True)
            self._shown = False


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
)
@click.version_option(version=_resolve_version(), prog_name="netdiag")
@click.option(
    "--lang",
    type=click.Choice(["ja", "en"]),
    default=None,
    help=H(
        "表示言語（既定: 環境変数 NETDIAG_LANG / ロケール、なければ日本語）",
        "Display language (default: NETDIAG_LANG / locale, else Japanese).",
    ),
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help=H(
        "netdiag の内部ログを標準エラーへ出力（既定は出力なし）",
        "Verbose netdiag logging to stderr (or --log-file). Quiet by default.",
    ),
)
@click.option(
    "--log-file",
    type=click.Path(),
    help=H(
        "ログを標準エラーではなくこのファイルへ出力",
        "Write netdiag logs to this file instead of stderr.",
    ),
)
@click.pass_context
def cli(
    ctx: click.Context, lang: Optional[str], verbose: bool, log_file: Optional[str]
) -> None:
    """netdiag — 設備ネットワークの診断 / network & device diagnosis.

    \b
    netdiag                        このPCのネットワークを診断（netdiag check と同じ）
    netdiag check 192.168.3.39:502 機器（PLC等）への到達性とポートを診断
    netdiag check -f devices.csv   台帳の全機器をまとめて診断
    netdiag scan                   同じネットワーク上の機器を一覧
    """
    setup(lang, verbose, log_file)
    if ctx.invoked_subcommand is None:
        try:
            ctx.invoke(ctx.command.get_command(ctx, "check"))
        except SystemExit:
            click.echo(_next_steps(), err=True)
            raise
