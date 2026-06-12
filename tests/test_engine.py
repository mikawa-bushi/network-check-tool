"""Tests for the diagnosis engine's root-cause / consequence separation.

We register synthetic probes whose outcomes we control, so we can assert that
the engine names the *lowest failing layer* as the cause and folds everything
that depends on it into 'consequences' — the property the whole tool rests on.
"""

import pytest

from netdiag.core import CheckResult, Context, Probe, Status, all_probes
from netdiag.core.probe import _REGISTRY
from netdiag.diagnosis import Engine


@pytest.fixture
def isolated_registry():
    """Swap the global registry for a clean one for the duration of a test."""
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
            return CheckResult(
                name=self.name, status=status, summary=f"{self.name} -> {status.value}"
            )

    _P.__name__ = f"Probe_{pname}"
    return _P


def _register(reg, *probes):
    for p in probes:
        reg[p.name] = p


def test_lowest_failure_is_root_cause(isolated_registry):
    # Chain: a (L2) <- b (L3) <- c (L7). b fails; a ok. c would fail but is a symptom.
    _register(
        isolated_registry,
        _make_probe("a", "L2", [], Status.OK),
        _make_probe("b", "L3", ["a"], Status.FAIL),
        _make_probe("c", "L7", ["b"], Status.FAIL),
    )
    diag = Engine().run()
    assert diag.primary is not None
    assert diag.primary.cause == "b"
    assert diag.primary.confidence == "high"
    # c was never run (its dependency b failed) -> SKIP, and counted as consequence.
    by_name = diag.results_by_name()
    assert by_name["c"].status == Status.SKIP
    assert "c" in diag.primary.consequences


def test_healthy_when_all_ok(isolated_registry):
    _register(
        isolated_registry,
        _make_probe("a", "L2", [], Status.OK),
        _make_probe("b", "L3", ["a"], Status.OK),
    )
    diag = Engine().run()
    assert diag.healthy
    assert diag.hypotheses == []


def test_two_independent_failures_give_two_hypotheses(isolated_registry):
    _register(
        isolated_registry,
        _make_probe("net", "L3", [], Status.FAIL),
        _make_probe("host", "SYSTEM", [], Status.FAIL),
    )
    diag = Engine().run()
    causes = {h.cause for h in diag.hypotheses}
    assert causes == {"net", "host"}
    # Independent failures => medium confidence, not high.
    assert all(h.confidence == "medium" for h in diag.hypotheses)


def test_warning_only_is_low_confidence_advisory(isolated_registry):
    _register(
        isolated_registry,
        _make_probe("a", "L2", [], Status.OK),
        _make_probe("b", "L3", ["a"], Status.WARN),
    )
    diag = Engine().run()
    assert not diag.healthy  # a warning is not fully healthy
    assert diag.primary.cause == "b"
    assert diag.primary.confidence == "low"


def test_topo_order_respects_dependencies(isolated_registry):
    _register(
        isolated_registry,
        _make_probe("c", "L7", ["b"], Status.OK),
        _make_probe("a", "L2", [], Status.OK),
        _make_probe("b", "L3", ["a"], Status.OK),
    )
    diag = Engine().run()
    order = [r.name for r in diag.results]
    assert order.index("a") < order.index("b") < order.index("c")
