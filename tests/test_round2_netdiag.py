"""Round-2 review fixes (2026-06-17): netdiag CLI / engine.

* U9  -- ``Engine.run`` takes an ``on_probe(name)`` progress hook, fired once per
  selected probe in the main thread for both the serial and parallel paths.
* U8  -- ``netdiag scenario`` gains ``--no-color`` (symmetry with ``check``).
* U10 -- the report-reasoning flag is ``--reasoning``; the word "verbose" is now
  reserved for the global logging flag (no command-level ``--verbose``).
"""

import pytest

from netdiag.cli import check, scenario
from netdiag.core import CheckResult, Probe, Status
from netdiag.core.probe import _REGISTRY
from netdiag.diagnosis import Engine


@pytest.fixture
def isolated_registry():
    saved = dict(_REGISTRY)
    _REGISTRY.clear()
    yield _REGISTRY
    _REGISTRY.clear()
    _REGISTRY.update(saved)


def _make_probe(pname, player, pdeps, status):
    class _P(Probe):
        name = pname
        title = pname
        layer = player
        depends_on = pdeps

        def check(self, ctx):
            return CheckResult(name=self.name, status=status, summary=pname)

    _P.__name__ = f"Probe_{pname}"
    return _P


def _register(reg, *probes):
    for p in probes:
        reg[p.name] = p


# ---------------------------------------------------------------------- U9


@pytest.mark.parametrize("jobs", [1, 4])
def test_engine_on_probe_fires_once_per_probe(isolated_registry, jobs):
    _register(
        isolated_registry,
        _make_probe("a", "L2", [], Status.OK),
        _make_probe("b", "L3", ["a"], Status.OK),
    )
    seen = []
    Engine().run(on_probe=seen.append, jobs=jobs)
    assert sorted(seen) == ["a", "b"]


def test_engine_on_probe_is_optional(isolated_registry):
    _register(isolated_registry, _make_probe("a", "L2", [], Status.OK))
    assert Engine().run().healthy  # no callback -> no error


# --------------------------------------------------------------------- U10


def test_check_uses_reasoning_not_command_verbose():
    names = {p.name for p in check.params}
    assert "reasoning" in names
    assert "verbose" not in names  # the only --verbose is the global group flag


# ---------------------------------------------------------------------- U8


def test_scenario_has_no_color_and_reasoning():
    names = {p.name for p in scenario.params}
    assert "no_color" in names
    assert "reasoning" in names
    assert "verbose" not in names
