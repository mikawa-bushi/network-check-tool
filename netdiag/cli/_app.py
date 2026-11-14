"""The ``netdiag`` click group and helpers shared by every subcommand."""

from __future__ import annotations

import inspect
import logging
import os
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import click

from .. import __version__
from ..core import InputError
from ..i18n import detect, set_language, tr

logger = logging.getLogger("netdiag.cli")

# click stores help texts when the commands are defined, before --lang is
# parsed. H() returns the environment's language and remembers both, so the
# help is rendered again in the language active when it is printed.
_HELP_JA = detect() == "ja"
_HELP_PAIRS: Dict[str, Tuple[str, str]] = {}


def H(ja: str, en: str) -> str:
    """Help text, printed in the language that --lang (or the environment) picks.

    Each text has one translation: a Japanese text paired with two English
    ones would print the other command's wording.
    """
    for text in (ja, en):
        for key in (text, inspect.cleandoc(text)):
            if _HELP_PAIRS.setdefault(key, (ja, en)) != (ja, en):
                raise ValueError(f"help text {key!r} has two translations")
    return ja if _HELP_JA else en


def _localized(text: Optional[str]) -> Optional[str]:
    pair = _HELP_PAIRS.get(text) if text else None
    return tr(*pair) if pair else text


def _lang_in(args: Sequence[str]) -> Optional[str]:
    for i, arg in enumerate(args):
        if arg.startswith("--lang="):
            return arg.partition("=")[2]
        if arg == "--lang" and i + 1 < len(args):
            return args[i + 1]
    return None


class _LocalizedHelp:
    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        self.help = _localized(self.help)
        for param in self.get_params(ctx):
            param.help = _localized(getattr(param, "help", None))
        super().format_help(ctx, formatter)

    def get_short_help_str(self, limit: int = 45) -> str:
        self.help = _localized(self.help)
        return super().get_short_help_str(limit)


class _Command(_LocalizedHelp, click.Command):
    pass


class _Group(_LocalizedHelp, click.Group):
    command_class = _Command
    group_class = type

    def parse_args(self, ctx: click.Context, args: List[str]) -> List[str]:
        # --lang has to hold before click parses the line, so that --help and a
        # usage error on the same line are already in that language.
        lang = _lang_in(args) if ctx.parent is None else None
        if lang:
            set_language(lang)
        return super().parse_args(ctx, args)


def _setup_logging(verbose: bool = False, log_file: Optional[str] = None) -> None:
    """Configure the ``netdiag`` logger for CLI use (library code never does this).

    Quiet by default: with neither ``verbose`` nor ``log_file`` set, no handler is
    attached and the package's NullHandler keeps things silent. Either one turns
    on the debug log: ``verbose`` to stderr, ``log_file`` to that file instead.
    """
    if not verbose and not log_file:
        return
    lg = logging.getLogger("netdiag")
    # Idempotent: if a real (non-Null) handler is already attached, don't stack
    # another one (avoids duplicate lines / leaked file handles on repeat calls).
    if any(not isinstance(h, logging.NullHandler) for h in lg.handlers):
        return
    handler: logging.Handler
    if log_file:
        try:
            handler = logging.FileHandler(log_file, encoding="utf-8")
        except OSError as e:
            raise InputError(
                tr(
                    f"ログファイルを開けません: {log_file}（{e.strerror or e}）",
                    f"cannot open the log file {log_file} ({e.strerror or e})",
                )
            ) from e
    else:
        handler = logging.StreamHandler()
    lg.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    lg.addHandler(handler)


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


def _report_bytes(text: str, fmt: str) -> bytes:
    """A report as it goes into a file or a pipe: always UTF-8.

    CSV gets a BOM so Excel reads Japanese, and keeps its own CRLF rows. Only
    the text report takes the platform's line ends, for old Notepad.
    """
    if fmt == "csv":
        return text.encode("utf-8-sig")
    if fmt == "text":
        text = text.replace("\n", os.linesep)
    return text.encode("utf-8")


def _print_report(text: str, fmt: str) -> None:
    """Text goes to the console as text. html/json/csv go out as the same bytes
    a file gets: redirected stdout on Windows would otherwise re-encode them to
    the ANSI code page and turn CSV's CRLF into CR CR LF."""
    if fmt == "text":
        click.echo(text)
        return
    data = _report_bytes(text, fmt)
    if not data.endswith(b"\n"):
        data += b"\n"
    sys.stdout.flush()
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def output_path(output: Optional[str], inputs: Iterable[str] = ()) -> Optional[str]:
    """Check ``-o`` before anything is measured. ``None`` and ``-`` mean stdout.

    Raises InputError for a folder that does not exist, and for a path that is
    one of *inputs*: a device list or scenario must never be overwritten by
    its report.
    """
    if output is None or output == "-":
        return None
    path = Path(output)
    if path.exists() and any(os.path.samefile(path, src) for src in inputs):
        raise InputError(
            tr(
                f"保存先 {output} は読み込むファイル（台帳・シナリオ）そのものです。"
                "上書きしないよう、レポートには別のファイル名を指定してください。",
                f"{output} is a file being read (a device list or scenario). "
                "Give the report another file name so it is not overwritten.",
            )
        )
    if not path.parent.is_dir():
        raise InputError(
            tr(
                f"保存先のフォルダーがありません: {path.parent}。"
                "先にフォルダーを作るか、別の場所を指定してください。",
                f"the folder to save into does not exist: {path.parent}. "
                "Create it first or choose another place.",
            )
        )
    return output


def log_file_apart_from(inputs: Iterable[str]) -> None:
    """Refuse a ``--log-file`` that is one of *inputs*, before anything is
    logged: a crashed check would append its traceback to the device list."""
    ctx = click.get_current_context(silent=True)
    log_file = ctx.find_root().params.get("log_file") if ctx is not None else None
    if not log_file or not Path(log_file).exists():
        return
    if any(os.path.samefile(log_file, src) for src in inputs):
        raise InputError(
            tr(
                f"ログファイル {log_file} は読み込むファイルそのものです。"
                "ログには別のファイル名を指定してください。",
                f"the log file {log_file} is a file being read. Give the log "
                "another file name.",
            )
        )


def write_output(text: str, output: Optional[str], fmt: str) -> None:
    """Print *text*, or save it to *output* (``-`` is stdout).

    A report that cannot be saved (the file is open in Excel, the disk is
    full) still reaches the user on stdout, and InputError says why (exit 4).
    """
    if output is None or output == "-":
        _print_report(text, fmt)
        return
    try:
        save_report(text, output, fmt)
    except OSError as e:
        _print_report(text, fmt)
        reason = e.strerror or str(e)
        raise InputError(
            tr(
                f"レポートを {output} に保存できませんでした（{reason}）。"
                "代わりに標準出力へ出しました。",
                f"could not save the report to {output} ({reason}). "
                "It went to standard output instead.",
            )
        ) from e


def save_report(text: str, output: str, fmt: str) -> None:
    """Save *text* to the file *output* as write_output would; raise OSError on failure."""
    Path(output).write_bytes(_report_bytes(text, fmt))
    click.echo(
        tr(f"レポートを保存しました: {output}", f"report written to {output}"),
        err=True,
    )


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


_WINDOWS = os.name == "nt"
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004


def _enable_vt(stream) -> bool:
    """Switch a Windows console to VT mode; False where it cannot be (pre-Win10)."""
    try:
        import ctypes
        import msvcrt

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = msvcrt.get_osfhandle(stream.fileno())  # type: ignore[attr-defined]
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        vt = _ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(mode.value & vt or kernel32.SetConsoleMode(handle, mode.value | vt))
    except (ImportError, AttributeError, OSError, ValueError):
        return False


def ansi_ok(stream) -> bool:
    """True when *stream* is a terminal that renders ANSI escape codes.

    A Windows 10 console prints them raw (``←[32m``) until VT processing is
    switched on, and click no longer does that through colorama.
    """
    try:
        if not stream.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    return not _WINDOWS or _enable_vt(stream)


def color_wanted(no_color: bool, err: bool = False) -> bool:
    """Colour for stdout (or stderr with *err*) unless --no-color."""
    return not no_color and ansi_ok(sys.stderr if err else sys.stdout)


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
    from .. import probes, runner  # noqa: F401  (register the built-in probes)
    from ..plugins import load_plugins

    set_language(lang)
    _setup_logging(verbose=verbose, log_file=log_file)
    load_plugins()


def _next_steps() -> str:
    from ..report.common import pad, width

    steps = [
        (
            tr(
                "同じネットワークの機器を探して台帳の下書きを作る",
                "find the devices on this network and draft a device list",
            ),
            tr("netdiag scan --save 台帳.csv", "netdiag scan --save devices.csv"),
        ),
        (
            tr(
                "台帳に名前を書き足したら、全機器をまとめて診断",
                "after naming them in the list, check every device",
            ),
            tr("netdiag check -f 台帳.csv", "netdiag check -f devices.csv"),
        ),
        (
            tr(
                "ときどき切れる原因を探す（台帳の機器を1時間測定）",
                "catch intermittent drops (measure the listed devices for 1 h)",
            ),
            tr(
                "netdiag soak -f 台帳.csv --duration 1h",
                "netdiag soak -f devices.csv --duration 1h",
            ),
        ),
        (tr("使えるコマンドの一覧", "list every command"), "netdiag --help"),
    ]
    cols = max(width(what) for what, _ in steps)
    lines = ["", tr("次にできること:", "What next:")]
    lines += [f"  {pad(what, cols)}  {cmd}" for what, cmd in steps]
    return "\n".join(lines)


class Progress:
    """One overwriting status line on stderr (only on a terminal that renders ANSI)."""

    def __init__(self, enabled: Optional[bool] = None) -> None:
        self.enabled = ansi_ok(sys.stderr) if enabled is None else enabled
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
    cls=_Group,
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
)
@click.version_option(version=__version__, prog_name="netdiag")
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
        "デバッグログ（内部エラーになったチェックのトレースバックなど）を標準エラーへ出力",
        "Write the debug log (such as the traceback of a check that crashed) to stderr.",
    ),
)
@click.option(
    "--log-file",
    type=click.Path(dir_okay=False),
    help=H(
        "デバッグログを標準エラーではなくこのファイルへ出力",
        "Write the debug log to this file instead of stderr.",
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
