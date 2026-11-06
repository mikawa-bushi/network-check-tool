"""Probe base class, execution context, and registry.

A probe answers one narrow question about one layer (is the NIC up? does the
gateway reply? does DNS resolve?). It declares which lower-layer probes it
``depends_on`` so the diagnosis engine can tell a *cause* from a *consequence*:
if the gateway is unreachable, a failing DNS probe is a symptom, not the root.

Probes come in two scopes:

* ``host`` probes describe *this* computer (NICs, gateway, DNS ...) and form the
  default sweep of ``netdiag check``.
* ``target`` probes describe one remote device (a PLC, HMI, robot controller)
  and run once per device, with the device in ``ctx.config["target"]``.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple, Type

from ..i18n import TextLike, render, render_list
from .result import CheckResult


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

    def network(self):
        """This host's network snapshot (shared, collected once per run)."""
        hn = self.config.get("_netinfo")
        if hn is None:
            from .. import netinfo

            hn = netinfo.snapshot()
            self.config["_netinfo"] = hn
        return hn


class Probe:
    """Base class. Subclasses set the class attributes and implement ``check``.

    ``check`` returns a :class:`CheckResult`. The base ``run`` wraps it with
    timing and a catch-all so a buggy or permission-denied probe degrades to a
    SKIP/FAIL result instead of aborting the whole diagnosis.

    Text attributes (``title``, ``what``, ``why``, ``means``, ``runbook``) may be
    plain strings or :class:`netdiag.i18n.Text` (bilingual, resolved lazily).
    """

    name: str = "probe"
    title: TextLike = ""
    layer: str = "?"  # L1, L2, L3, L4, L7, SYSTEM
    depends_on: List[str] = []
    requires_root: bool = False
    #: ``host`` (this computer) or ``target`` (one remote device).
    scope: str = "host"
    #: Included in the default sweep? Opt-in probes (traceroute ...) say False.
    default: bool = True
    #: Only meaningful when the network is expected to reach the internet.
    requires_internet: bool = False
    # What/why the probe checks and the manual commands that do the same thing.
    # Surfaced verbatim by ``netdiag explain`` so users can learn to do it by hand.
    what: TextLike = ""
    why: TextLike = ""
    manual_commands: List[TextLike] = []
    # Diagnosis narrative for when THIS probe is the root cause: what it means
    # and what to do next. ``advice()`` may override this with steps built from
    # the actual result (the real gateway IP, the device name ...). ``means``
    # falls back to ``why`` and ``runbook`` to ``manual_commands`` when unset.
    means: TextLike = ""
    runbook: List[TextLike] = []

    def check(self, ctx: Context) -> CheckResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def advice(
        self,
        result: Optional[CheckResult],
        results: Optional[Dict[str, CheckResult]] = None,
    ) -> Tuple[str, List[str]]:
        """``(what it means, next steps)`` when this probe is the root cause.

        ``result`` is None when asked statically (``netdiag explain``, runbook
        lookups without a run); overrides must handle that.
        """
        means = render(self.means) or render(self.why)
        steps = render_list(self.runbook) or render_list(self.manual_commands)
        return means, steps

    def run(self, ctx: Context) -> CheckResult:
        start = time.perf_counter()
        try:
            res = self.check(ctx)
        except Exception as e:  # never let one probe kill the run
            res = CheckResult.skip(
                self.name,
                f"probe error: {e}",
                error=str(e),
                code=f"{self.name}.error",
            )
        res.layer = res.layer if res.layer != "?" else self.layer
        res.title = render(res.title) or render(self.title) or self.name
        if not res.depends_on:
            res.depends_on = list(self.depends_on)
        if res.raw_command is None and self.manual_commands:
            res.raw_command = " ; ".join(render_list(self.manual_commands))
        if not res.code:
            res.code = f"{self.name}.{res.status.value}"
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
