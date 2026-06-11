"""Probe base class, execution context, and registry.

A probe answers one narrow question about one layer (is the NIC up? does the
gateway reply? does DNS resolve?). It declares which lower-layer probes it
``depends_on`` so the diagnosis engine can tell a *cause* from a *consequence*:
if the gateway is unreachable, a failing DNS probe is a symptom, not the root.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Type

from .result import CheckResult, Status


class Context:
    """Shared state for one diagnosis run.

    Probes read ``config`` for targets/resolvers and may inspect ``results`` for
    the outcome of probes that already ran (their declared dependencies), e.g.
    the DNS probe reads the gateway result to phrase its summary.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config: Dict[str, Any] = config or {}
        self.results: Dict[str, CheckResult] = {}

    def result(self, name: str) -> Optional[CheckResult]:
        return self.results.get(name)

    def get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)


class Probe:
    """Base class. Subclasses set the class attributes and implement ``check``.

    ``check`` returns a :class:`CheckResult`. The base ``run`` wraps it with
    timing and a catch-all so a buggy or permission-denied probe degrades to a
    SKIP/FAIL result instead of aborting the whole diagnosis.
    """

    name: str = "probe"
    title: str = ""
    layer: str = "?"  # L1, L2, L3, L4, L7, SYSTEM
    depends_on: List[str] = []
    requires_root: bool = False
    # What/why the probe checks and the manual commands that do the same thing.
    # Surfaced verbatim by ``netdiag explain`` so users can learn to do it by hand.
    what: str = ""
    why: str = ""
    manual_commands: List[str] = []

    def check(self, ctx: Context) -> CheckResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def run(self, ctx: Context) -> CheckResult:
        start = time.perf_counter()
        try:
            res = self.check(ctx)
        except Exception as e:  # never let one probe kill the run
            res = CheckResult.skip(
                self.name, f"probe error: {e}", error=str(e),
            )
        res.layer = res.layer if res.layer != "?" else self.layer
        res.title = res.title or self.title or self.name
        if not res.depends_on:
            res.depends_on = list(self.depends_on)
        if res.raw_command is None and self.manual_commands:
            res.raw_command = " ; ".join(self.manual_commands)
        res.duration_ms = (time.perf_counter() - start) * 1000.0
        return res


# --- registry ---------------------------------------------------------------

_REGISTRY: Dict[str, Type[Probe]] = {}


def register(cls: Type[Probe]) -> Type[Probe]:
    """Class decorator: add a probe to the global registry by its ``name``."""
    _REGISTRY[cls.name] = cls
    return cls


def get_probe(name: str) -> Optional[Type[Probe]]:
    return _REGISTRY.get(name)


def all_probes() -> Dict[str, Type[Probe]]:
    return dict(_REGISTRY)


def registry_names() -> List[str]:
    return sorted(_REGISTRY)
