"""Shared presentation helpers: symbols, colours, widths, status wording."""

from __future__ import annotations

import codecs
import sys
import unicodedata
from typing import Optional, Tuple

from ..core import CheckResult, Status
from ..i18n import tr

_COLORS = {
    Status.OK: "\033[32m",
    Status.WARN: "\033[33m",
    Status.FAIL: "\033[31m",
    Status.UNKNOWN: "\033[35m",
    Status.SKIP: "\033[90m",
}
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

_ASCII_SYMBOLS = {
    Status.OK: "OK",
    Status.WARN: "!!",
    Status.FAIL: "NG",
    Status.UNKNOWN: "??",
    Status.SKIP: "--",
}

# Result codes ending like this mean "nothing to check here" (no URL given,
# not on Wi-Fi, tool missing ...). They are hidden from the default terminal
# view to keep the report about the network, and kept in JSON/HTML.
_NA_SUFFIXES = (
    ".none",
    ".no_target",
    ".no_tool",
    ".unsupported",
    ".need_root",
    ".unavailable",
    ".untested",
    ".no_wired",
    ".no_host",
)


def is_not_applicable(r: CheckResult) -> bool:
    return r.status == Status.SKIP and (r.code or "").endswith(_NA_SUFFIXES)


def unicode_ok(stream=None) -> bool:
    """Can *stream* encode the status symbols? (cp932 consoles cannot)"""
    stream = stream or sys.stdout
    enc = getattr(stream, "encoding", None) or "utf-8"
    try:
        "✔⚠✘—━■".encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


class Style:
    """Colour + symbol choices for one rendering."""

    def __init__(self, color: bool = False, unicode: Optional[bool] = None) -> None:
        self.color = color
        self.unicode = unicode_ok() if unicode is None else unicode

    def c(self, code: str) -> str:
        return code if self.color else ""

    def sym(self, status: Status) -> str:
        return status.symbol if self.unicode else _ASCII_SYMBOLS[status]

    def status(self, status: Status, text: Optional[str] = None) -> str:
        body = text if text is not None else self.sym(status)
        return f"{self.c(_COLORS.get(status, ''))}{body}{self.c(RESET)}"

    def bold(self, text: str) -> str:
        return f"{self.c(BOLD)}{text}{self.c(RESET)}"

    def dim(self, text: str) -> str:
        return f"{self.c(DIM)}{text}{self.c(RESET)}"

    @property
    def rule(self) -> str:
        return "━━━" if self.unicode else "==="

    @property
    def bullet(self) -> str:
        return "■" if self.unicode else "#"


# A terminal on an East Asian encoding (LANG=ja_JP.eucJP) draws every
# double-byte character two cells wide, including the "ambiguous" ones such as
# "—", "≤", "…" and the stand-ins "―" and "≦" that replace them there.
_EAST_ASIAN_CODECS = frozenset(
    {
        "cp932",
        "shift_jis",
        "shift_jis_2004",
        "shift_jisx0213",
        "euc_jp",
        "euc_jis_2004",
        "euc_jisx0213",
        "iso2022_jp",
        "gbk",
        "gb2312",
        "gb18030",
        "cp949",
        "euc_kr",
        "johab",
        "cp950",
        "big5",
        "big5hkscs",
    }
)


def _wide_classes() -> Tuple[str, ...]:
    """East Asian width classes that take two cells on this process's stdout."""
    try:
        encoding = codecs.lookup(getattr(sys.stdout, "encoding", None) or "utf-8")
    except LookupError:
        return ("W", "F")
    return ("W", "F", "A") if encoding.name in _EAST_ASIAN_CODECS else ("W", "F")


def _cells(ch: str, wide: Tuple[str, ...]) -> int:
    return 2 if unicodedata.east_asian_width(ch) in wide else 1


def width(text: str) -> int:
    """Display width in terminal cells (CJK full-width characters take 2)."""
    wide = _wide_classes()
    return sum(_cells(ch, wide) for ch in text)


def pad(text: str, cols: int) -> str:
    return text + " " * max(0, cols - width(text))


def truncate(text: str, cols: int) -> str:
    if width(text) <= cols:
        return text
    wide = _wide_classes()
    room = cols - _cells("…", wide)
    out, w = "", 0
    for ch in text:
        cw = _cells(ch, wide)
        if w + cw > room:
            break
        out += ch
        w += cw
    return out + "…"


def status_word(status: Status) -> str:
    return {
        Status.OK: tr("正常", "OK"),
        Status.WARN: tr("注意", "Warning"),
        Status.FAIL: tr("異常", "Problem"),
        Status.UNKNOWN: tr("測定不可", "Not measured"),
        Status.SKIP: tr("未評価", "Not checked"),
    }[status]


def confidence_word(conf: str) -> str:
    return {
        "high": tr("高", "high"),
        "medium": tr("中", "medium"),
        "low": tr("低", "low"),
    }.get(conf, conf)
