"""Core data model: the *facts* a probe reports.

A probe never decides *why* something is broken; it only records *what* it
observed. Interpretation is the job of :mod:`netdiag.diagnosis`. Keeping the two
separate is what lets the same measurements feed the terminal report, the JSON
output, the baseline learner and the diagnosis engine without coupling.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum, IntEnum
from typing import Any, Dict, Iterable, List, Optional


class ExitCode(IntEnum):
    """Process exit status of every netdiag command."""

    OK = 0  # healthy
    FAIL = 1  # a problem was found
    WARN = 2  # warnings only, and everything wanted was measured
    INCOMPLETE = 3  # no problem found, but something wanted was not measured
    USAGE = 4  # bad option, unknown check name, unusable input or output file
    INTERNAL = 5  # an unexpected exception inside netdiag
    INTERRUPTED = 130  # Ctrl-C before a verdict existed


class Status(str, Enum):
    """Outcome of a single check, and the overall verdict of a run.

    ``str`` mix-in so it serialises to plain JSON and compares to literals.

    * ``UNKNOWN``: the check applies and was wanted, but no verdict could be
      measured (the probe crashed, its tool is missing, its output could not be
      read, or nothing was measured at all).
    * ``SKIP``: not applicable here, excluded by the user, or not evaluated
      because an upstream check already failed. It never changes the verdict.
    """

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    UNKNOWN = "unknown"
    SKIP = "skip"

    @property
    def symbol(self) -> str:
        return _SYMBOLS[self]

    @property
    def exit_code(self) -> ExitCode:
        """Exit code for this status as the overall verdict of a run."""
        return _EXIT_CODES[self]

    @classmethod
    def worst(cls, statuses: Iterable["Status"]) -> "Status":
        """The overall verdict: FAIL > UNKNOWN > WARN > OK.

        SKIP never escalates. When nothing produced a verdict (no statuses, or
        only SKIP), nothing was measured, so the verdict is UNKNOWN.
        """
        seen = set(statuses)
        for s in _PRECEDENCE:
            if s in seen:
                return s
        return cls.UNKNOWN


_SYMBOLS = {
    Status.OK: "✔",
    Status.WARN: "⚠",
    Status.FAIL: "✘",
    Status.UNKNOWN: "?",
    Status.SKIP: "—",
}
_EXIT_CODES = {
    Status.OK: ExitCode.OK,
    Status.WARN: ExitCode.WARN,
    Status.FAIL: ExitCode.FAIL,
    Status.UNKNOWN: ExitCode.INCOMPLETE,
    Status.SKIP: ExitCode.OK,
}
_PRECEDENCE = (Status.FAIL, Status.UNKNOWN, Status.WARN, Status.OK)


@dataclass
class CheckResult:
    """The result of running one probe.

    ``metrics`` holds numeric, machine-comparable values (rtt_ms, loss_pct ...)
    that baselining and change-point detection consume. ``detail`` holds richer
    structured evidence for the report. ``raw_command`` is the equivalent manual
    command, surfaced by ``netdiag explain`` so users learn to diagnose by hand.
    ``summary`` is human text in the active display language; ``code`` is the
    stable, language-independent outcome key (``gateway.unreachable`` ...) that
    scripts should match on instead of the wording.
    """

    name: str
    status: Status
    summary: str
    layer: str = "?"
    title: str = ""
    metrics: Dict[str, float] = field(default_factory=dict)
    detail: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    raw_command: Optional[str] = None
    error: Optional[str] = None
    duration_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)
    code: str = ""

    # convenience constructors -------------------------------------------------
    @classmethod
    def ok(cls, name: str, summary: str, **kw: Any) -> "CheckResult":
        return cls(name=name, status=Status.OK, summary=summary, **kw)

    @classmethod
    def warn(cls, name: str, summary: str, **kw: Any) -> "CheckResult":
        return cls(name=name, status=Status.WARN, summary=summary, **kw)

    @classmethod
    def fail(cls, name: str, summary: str, **kw: Any) -> "CheckResult":
        return cls(name=name, status=Status.FAIL, summary=summary, **kw)

    @classmethod
    def skip(cls, name: str, summary: str, **kw: Any) -> "CheckResult":
        return cls(name=name, status=Status.SKIP, summary=summary, **kw)

    @classmethod
    def unknown(cls, name: str, summary: str, **kw: Any) -> "CheckResult":
        return cls(name=name, status=Status.UNKNOWN, summary=summary, **kw)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["title"] = str(self.title)
        return d
