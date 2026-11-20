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

ENTRY_POINT_GROUP = "netdiag.probes"


def load_plugins() -> None:
    """Import every registered plugin module, skipping any that fails to load."""
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            target = ep.load()
            # If the entry point is a module, importing already ran @register.
            if callable(target):
                target()
        except Exception:  # never let a plugin break the tool
            pass
