"""Per-cause explanations and next steps.

The engine decides *which* probe is the root cause; the probe itself supplies
the human-facing "what this means and what to do next" through
:meth:`netdiag.core.Probe.advice`. Keeping the narrative next to the check that
produces the evidence lets it quote the actual values (the gateway's address,
the device's name, the NIC that is half-duplex) in the user's language, and
plugin probes get the same treatment for free.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from ..core import CheckResult, Status, get_probe
from ..i18n import tr


def advice_for(
    result: Optional[CheckResult],
    by_name: Optional[Dict[str, CheckResult]] = None,
    cause: Optional[str] = None,
) -> Tuple[str, List[str]]:
    """``(means, steps)`` for a result (or, statically, for a probe name)."""
    name = cause or (result.name if result is not None else None)
    cls = get_probe(name) if name else None
    if cls is None:
        return (result.summary if result is not None else ""), []
    try:
        means, steps = cls().advice(result, by_name or {})
    except Exception:  # a buggy advice() must not hide the diagnosis itself
        means, steps = "", []
    return means, list(steps)


def explain_failure(
    root: CheckResult, by_name: Dict[str, CheckResult]
) -> Tuple[str, List[str]]:
    """Build a one-line cause summary plus supporting evidence bullets."""
    means = advice_for(root, by_name)[0] or root.summary
    evidence = [root.summary]
    # Cite the OK dependency that makes this the root (e.g. gateway OK -> dns is cause).
    for dep in root.depends_on:
        d = by_name.get(dep)
        if d and d.status == Status.OK:
            evidence.append(
                tr(
                    f"前提の『{d.title}』は正常（{d.summary}）— 原因はその先にあります",
                    f"{d.title} is OK ({d.summary}), so the fault is above it.",
                )
            )
    return means, evidence


def runbook_steps(cause: str) -> List[str]:
    """Generic next steps for a probe name (no run result available)."""
    return advice_for(None, cause=cause)[1]


def _runbook(cause: str) -> Dict:
    """``{"means", "steps"}`` for a probe name; ``{}`` when unknown."""
    if get_probe(cause) is None:
        return {}
    means, steps = advice_for(None, cause=cause)
    return {"means": means, "steps": steps}
