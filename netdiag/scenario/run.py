"""Run a scenario as a ``netdiag check`` with the scenario's settings."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from .loader import Scenario, ScenarioPass


def run_scenario(
    scenario: Scenario,
    config: Dict[str, Any],
    progress: Optional[Callable[[str], None]] = None,
    on_probe: Optional[Callable[[str], None]] = None,
):
    """Every check in the scenario runs once, with its own settings.

    The first pass goes through :func:`netdiag.runner.run_check` with the
    devices; the checks that could not share it (a probe listed twice, or
    conflicting settings) run in further passes and join the host diagnosis.
    Returns a :class:`netdiag.runner.CheckReport`.
    """
    from .. import runner
    from ..diagnosis import Engine

    first, *rest = scenario.passes() or [ScenarioPass()]
    rep = runner.run_check(
        scenario.devices,
        config={**config, **first.config},
        only=first.probe_names or None,
        progress=progress,
        on_probe=on_probe,
    )
    first.apply_labels(rep.host.results)
    if rest:
        results, reasoning = list(rep.host.results), list(rep.host.reasoning)
        for p in rest:
            diag = Engine(config={**config, **p.config}, verbose=True).run(
                only=p.probe_names, on_probe=on_probe
            )
            p.apply_labels(diag.results)
            results += diag.results
            reasoning += diag.reasoning
        rep.host = Engine(config=config).diagnose(results, reasoning)
    rep.settings["scenario"] = scenario.name
    return rep
