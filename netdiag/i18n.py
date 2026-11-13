"""Bilingual (Japanese / English) human-facing text.

netdiag is built for production engineers on the shop floor, so the default
language is Japanese; English is selected by ``--lang en``, ``NETDIAG_LANG=en``
or an English locale. Only *human* text is localised — machine-readable output
(JSON keys, result ``code`` values, metric names) is always English so scripts
never depend on the display language.

Two helpers cover every call site:

* :func:`tr` picks between two already-formatted strings at run time
  (``tr(f"{gw} が応答しません", f"{gw} is not responding")``).
* :class:`Text` is the lazy form for class attributes evaluated at import time
  (probe titles, explanations); it renders in the language active when it is
  formatted, so changing the language later still takes effect.
"""

from __future__ import annotations

import locale
import os
import threading
from typing import Iterable, List, Mapping, Optional, Union

SUPPORTED = ("ja", "en")
DEFAULT = "ja"

_lock = threading.Lock()
_current: Optional[str] = None


def normalize(value: Optional[str]) -> Optional[str]:
    """Map a user/locale value (``ja_JP.UTF-8``, ``English``...) to ja/en/None."""
    if not value:
        return None
    v = value.strip().lower()
    if v.startswith(("ja", "japanese")):
        return "ja"
    if v.startswith(("en", "english")):
        return "en"
    return None


def detect(env: Optional[Mapping[str, str]] = None) -> str:
    """Resolve the display language: NETDIAG_LANG > locale env vars > OS locale.

    Falls back to Japanese (the target audience) when nothing says English, so a
    bare ``C``/``POSIX`` locale on a shop-floor PC still gets Japanese output.
    """
    env = os.environ if env is None else env
    explicit = normalize(env.get("NETDIAG_LANG"))
    if explicit:
        return explicit
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        lang = normalize(env.get(var))
        if lang:
            return lang
    return _os_language() or DEFAULT


def _os_language() -> Optional[str]:
    """The OS locale's language (on Windows, e.g. ``Japanese_Japan.932``).

    The C/POSIX locale names no language. Python coerces it to ``C.UTF-8``,
    which ``getlocale()`` on 3.11 reports as ``en_US``, so it is checked first.
    """
    try:
        raw = locale.setlocale(locale.LC_CTYPE)
        if raw.split(".")[0].upper() in ("C", "POSIX"):
            return None
        return normalize(locale.getlocale()[0])
    except (locale.Error, ValueError, TypeError):  # pragma: no cover - exotic setups
        return None


def set_language(lang: Optional[str]) -> str:
    """Set the process-wide display language (``None`` re-detects). Returns it."""
    global _current
    resolved = normalize(lang) or detect()
    with _lock:
        _current = resolved
    return resolved


def get_language() -> str:
    global _current
    if _current is None:
        with _lock:
            if _current is None:
                _current = detect()
    return _current


def is_ja() -> bool:
    return get_language() == "ja"


def tr(ja: str, en: str) -> str:
    """Return the string for the active language."""
    return ja if is_ja() else en


class Text:
    """A lazily localised string for import-time constants.

    Behaves like ``str`` where it matters for rendering (``str()``, f-strings,
    ``len``, truthiness), but resolves to the active language each time.
    """

    __slots__ = ("ja", "en")

    def __init__(self, ja: str, en: str) -> None:
        self.ja = ja
        self.en = en

    def __str__(self) -> str:
        return self.ja if is_ja() else self.en

    def __format__(self, spec: str) -> str:
        return format(str(self), spec)

    def __len__(self) -> int:
        return len(str(self))

    def __bool__(self) -> bool:
        return bool(self.ja or self.en)

    def __repr__(self) -> str:
        return f"Text(ja={self.ja!r}, en={self.en!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Text):
            return (self.ja, self.en) == (other.ja, other.en)
        if isinstance(other, str):
            return str(self) == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.ja, self.en))


TextLike = Union[str, Text]


def render(value: Optional[TextLike]) -> str:
    """``str()`` that tolerates ``None`` (plugin probes may leave fields unset)."""
    return "" if value is None else str(value)


def render_list(values: Optional[Iterable[TextLike]]) -> List[str]:
    return [str(v) for v in (values or [])]
