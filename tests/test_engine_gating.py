"""The dependency gate hides a check only when a failure lies on its own path."""

import pytest

from netdiag.core import CheckResult, Probe, Status
from netdiag.core.probe import _REGISTRY
from netdiag.diagnosis import Engine
from netdiag.report import render_terminal


@pytest.fixture
def registry():
    saved = dict(_REGISTRY)
    _REGISTRY.clear()
    yield _REGISTRY
    _REGISTRY.clear()
    _REGISTRY.update(saved)


def _probe(pname, status, deps=()):
    class _P(Probe):
        name = pname
        title = pname
        layer = "L3"
        depends_on = list(deps)

        def check(self, ctx):
            return CheckResult(name=pname, status=status, summary=f"{pname} measured")

    return _P


class _Port(Probe):
    """A port check whose host needs DNS only when it is a name."""

    name = "port"
    title = "port"
    layer = "L4"
    depends_on = ["dns"]

    def path_dependencies(self, ctx):
        host = ctx.get("port_host", "")
        return [] if host.replace(".", "").isdigit() else ["dns"]

    def check(self, ctx):
        return CheckResult.fail(self.name, f"{ctx.get('port_host')}:502 refused")


@pytest.fixture
def broken_internet(registry):
    """No gateway, so nothing off this LAN can be measured."""
    for p in (
        _probe("gw", Status.FAIL),
        _probe("ext", Status.OK, ["gw"]),
        _probe("dns", Status.OK, ["ext"]),
        _Port,
    ):
        registry[p.name] = p
    return registry


def test_a_check_on_an_unrelated_target_still_runs(broken_internet):
    diag = Engine(config={"port_host": "192.0.2.39"}).run()
    port = diag.results_by_name()["port"]
    assert port.status == Status.FAIL
    assert port.summary == "192.0.2.39:502 refused"
    causes = {h.cause: h for h in diag.hypotheses}
    assert set(causes) == {"gw", "port"}
    assert "port" not in causes["gw"].consequences


def test_a_check_on_the_failed_path_is_not_evaluated(broken_internet):
    diag = Engine(config={"port_host": "plc1.example"}).run()
    port = diag.results_by_name()["port"]
    assert port.status == Status.SKIP
    assert port.code == "port.not_evaluated"
    assert [h.cause for h in diag.hypotheses] == ["gw"]
    assert "port" in diag.primary.consequences


def test_not_evaluated_names_the_check_that_failed(broken_internet):
    diag = Engine(config={"port_host": "plc1.example"}).run()
    by = diag.results_by_name()
    assert by["dns"].summary == "not evaluated: upstream gw failed"
    assert by["port"].summary == "not evaluated: upstream gw failed"
    assert "upstream dns failed" not in render_terminal(diag, color=False)


def test_an_unmeasured_dependency_does_not_hide_its_dependents(registry):
    for p in (_probe("gw", Status.UNKNOWN), _probe("ext", Status.FAIL, ["gw"])):
        registry[p.name] = p
    diag = Engine().run()
    assert diag.results_by_name()["ext"].status == Status.FAIL
    assert diag.primary.cause == "ext"
