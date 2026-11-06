"""Core data model: the *facts* a probe reports.

A probe never decides *why* something is broken; it only records *what* it
observed. Interpretation is the job of :mod:`netdiag.diagnosis`. Keeping the two
separate is what lets the same measurements feed the terminal report, the JSON
output, the baseline learner and the diagnosis engine without coupling.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional


class Status(str, Enum):
    """Outcome of a single check.

    ``str`` mix-in so it serialises to plain JSON and compares to literals.
    """

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"  # could not run (missing tool, no permission, N/A on platform)

    @property
    def symbol(self) -> str:
        return {
            Status.OK: "✔",  # ✔
            Status.WARN: "⚠",  # ⚠
            Status.FAIL: "✘",  # ✘
            Status.SKIP: "—",  # —
        }[self]

    @property
    def exit_code(self) -> int:
        """CLI exit code for an overall verdict: 0 healthy, 1 failure, 2 warnings only."""
        return {Status.FAIL: 1, Status.WARN: 2}.get(self, 0)

    @classmethod
    def worst(cls, statuses: Iterable["Status"]) -> "Status":
        """FAIL if any failed, else WARN if any warned, else OK (SKIP never escalates)."""
        seen = set(statuses)
        for s in (cls.FAIL, cls.WARN):
            if s in seen:
                return s
        return cls.OK


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

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["title"] = str(self.title)
        return d
