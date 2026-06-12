"""Subprocess and platform helpers shared by probes.

The guiding rule for the whole project: *never crash on a missing tool or a
denied permission*. A probe that cannot run must degrade to ``Status.SKIP`` with
a human reason, so a diagnosis on a locked-down host is still useful instead of a
stack trace.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional


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


def run(args: List[str], timeout: float = 10.0) -> CommandResult:
    """Run *args* without a shell, capturing output and never raising."""
    if not args or shutil.which(args[0]) is None:
        return CommandResult(
            ok=False,
            code=127,
            stdout="",
            stderr=f"{args[0] if args else '?'}: not found",
            found=False,
        )
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return CommandResult(
            ok=proc.returncode == 0,
            code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            found=True,
        )
    except subprocess.TimeoutExpired as e:
        return CommandResult(
            ok=False,
            code=124,
            stdout=e.stdout or "",
            stderr="timed out",
            found=True,
            timed_out=True,
        )
    except OSError as e:  # permission, etc.
        return CommandResult(ok=False, code=126, stdout="", stderr=str(e), found=True)


def is_linux() -> bool:
    return platform.system() == "Linux"


def is_macos() -> bool:
    return platform.system() == "Darwin"


def is_windows() -> bool:
    return platform.system() == "Windows"


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
