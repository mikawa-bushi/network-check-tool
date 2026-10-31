"""Subprocess and platform helpers shared by probes.

The guiding rule for the whole project: *never crash on a missing tool or a
denied permission*. A probe that cannot run must degrade to ``Status.SKIP`` with
a human reason, so a diagnosis on a locked-down host is still useful instead of a
stack trace.
"""

from __future__ import annotations

import locale
import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class CommandResult:
    ok: bool
    code: int
    stdout: str
    stderr: str
    found: bool  # was the executable present at all?
    timed_out: bool = False

    @property
    def text(self) -> str:
        return self.stdout if self.stdout else self.stderr


def have(cmd: str) -> bool:
    """Is *cmd* available on PATH?"""
    return shutil.which(cmd) is not None


def decode_output(data: Optional[bytes]) -> str:
    """Decode command output without ever raising.

    Tries UTF-8 first (Linux/macOS, PowerShell with UTF-8 output), then the
    console/ANSI code page (cp932 on Japanese Windows, where ``ping``/``arp``
    print Shift_JIS), and finally replaces undecodable bytes.
    """
    if not data:
        return ""
    if isinstance(data, str):  # pragma: no cover - defensive
        return data
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for enc in _fallback_encodings():
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _fallback_encodings() -> List[str]:
    encs: List[str] = []
    if is_windows():
        try:
            import ctypes

            encs.append(f"cp{ctypes.windll.kernel32.GetOEMCP()}")  # console CP
        except (AttributeError, OSError):  # pragma: no cover - non-Windows
            pass
    preferred = locale.getpreferredencoding(False)
    if preferred:
        encs.append(preferred)
    encs.append("cp932")  # common on Japanese shop-floor PCs
    return encs


def _command_env() -> Dict[str, str]:
    """Environment for child processes.

    On Linux/macOS force the C locale so tools print stable English output that
    the parsers understand. Windows tools are localised regardless of env vars,
    so their parsers are written to be language-independent instead.
    """
    env = dict(os.environ)
    if not is_windows():
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
    return env


@dataclass
class RawResult:
    """Undecoded outcome of one command; ``returncode`` is None if it never finished."""

    returncode: Optional[int]
    stdout: bytes = b""
    stderr: bytes = b""
    seconds: float = 0.0
    found: bool = True
    timed_out: bool = False


def run_bytes(args: List[str], timeout: float = 10.0) -> RawResult:
    """Run *args* without a shell or decoding, capturing output; never raises."""
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=_command_env(),
            **_no_window_kwargs(),
        )
        return RawResult(
            proc.returncode, proc.stdout, proc.stderr, time.perf_counter() - start
        )
    except FileNotFoundError as e:
        return RawResult(None, b"", str(e).encode(), found=False)
    except subprocess.TimeoutExpired as e:
        return RawResult(
            None, e.stdout or b"", e.stderr or b"", timeout, timed_out=True
        )
    except OSError as e:  # permission, etc.
        return RawResult(None, b"", str(e).encode(), time.perf_counter() - start)


def run(args: List[str], timeout: float = 10.0) -> CommandResult:
    """Run *args* without a shell, capturing decoded output and never raising."""
    if not args or shutil.which(args[0]) is None:
        return CommandResult(
            ok=False,
            code=127,
            stdout="",
            stderr=f"{args[0] if args else '?'}: not found",
            found=False,
        )
    r = run_bytes(args, timeout)
    if r.timed_out:
        return CommandResult(
            ok=False,
            code=124,
            stdout=decode_output(r.stdout),
            stderr="timed out",
            found=True,
            timed_out=True,
        )
    if r.returncode is None:
        return CommandResult(
            ok=False, code=126, stdout="", stderr=decode_output(r.stderr), found=True
        )
    return CommandResult(
        ok=r.returncode == 0,
        code=r.returncode,
        stdout=decode_output(r.stdout),
        stderr=decode_output(r.stderr),
        found=True,
    )


def _no_window_kwargs() -> dict:
    """Keep console windows from flashing when launched from a GUI on Windows."""
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {"creationflags": flag} if is_windows() and flag else {}


def is_linux() -> bool:
    return platform.system() == "Linux"


def is_macos() -> bool:
    return platform.system() == "Darwin"


def is_windows() -> bool:
    return platform.system() == "Windows"


def os_name() -> str:
    """``linux`` / ``windows`` / ``macos`` / ``other``."""
    if is_linux():
        return "linux"
    if is_windows():
        return "windows"
    if is_macos():
        return "macos"
    return "other"


def is_root() -> bool:
    try:
        return os.geteuid() == 0  # type: ignore[attr-defined]
    except AttributeError:  # Windows
        return False


def first_match(text: str, *needles: str) -> Optional[str]:
    for line in text.splitlines():
        for n in needles:
            if n in line:
                return line.strip()
    return None
