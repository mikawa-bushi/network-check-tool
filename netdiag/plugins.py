"""Third-party probe discovery via entry points.

Keeps the core stdlib-only while letting heavier or site-specific probes ship as
separate packages. A plugin package declares, in its own pyproject:

    [project.entry-points."netdiag.probes"]
    my_probe = "mypkg.module"

Importing the named module is enough — its ``@register`` decorators add the
probes to the global registry, exactly like the built-ins.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import List, Tuple

ENTRY_POINT_GROUP = "netdiag.probes"


def load_plugins() -> List[Tuple[str, bool, str]]:
    """Import every registered plugin module.

    Returns a list of (name, ok, message) so the CLI can report what loaded and
    what failed without aborting on a single bad plugin.
    """
    results: List[Tuple[str, bool, str]] = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            target = ep.load()
            # If the entry point is a module, importing already ran @register.
            if callable(target):
                target()
            results.append((ep.name, True, "loaded"))
        except Exception as e:  # never let a plugin break the tool
            results.append((ep.name, False, str(e)))
    return results
