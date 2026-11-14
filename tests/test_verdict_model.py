"""The verdict model: statuses, their precedence, and the exit codes users see."""

import json

import pytest

from netdiag.core import CheckResult, ExitCode, Probe, Status
from netdiag.core.probe import _REGISTRY
from netdiag.diagnosis import Engine
from netdiag.report import render_json, render_terminal


@pytest.fixture
def registry():
    saved = dict(_REGISTRY)
    _REGISTRY.clear()
    yield _REGISTRY
    _REGISTRY.clear()
    _REGISTRY.update(saved)


def _probe(pname, status=Status.OK, deps=(), raises=None):
    class _P(Probe):
        name = pname
        title = pname
        layer = "L3"
        depends_on = list(deps)

        def check(self, ctx):
            if raises is not None:
                raise raises
            return CheckResult(name=pname, status=status, summary=status.value)

    return _P


def _register(reg, *probes):
    for p in probes:
        reg[p.name] = p


@pytest.mark.parametrize(
    "statuses, verdict",
    [
        ([Status.OK, Status.WARN, Status.UNKNOWN, Status.FAIL], Status.FAIL),
        ([Status.OK, Status.WARN, Status.UNKNOWN], Status.UNKNOWN),
        ([Status.OK, Status.WARN], Status.WARN),
        ([Status.OK, Status.SKIP], Status.OK),
        ([Status.WARN, Status.SKIP], Status.WARN),
        ([Status.SKIP, Status.SKIP], Status.UNKNOWN),
        ([], Status.UNKNOWN),
    ],
)
def test_overall_verdict_precedence(statuses, verdict):
    assert Status.worst(statuses) == verdict


def test_exit_codes_follow_the_documented_table():
    assert [
        Status.OK.exit_code,
        Status.FAIL.exit_code,
        Status.WARN.exit_code,
        Status.UNKNOWN.exit_code,
    ] == [0, 1, 2, 3]
    assert [int(c) for c in ExitCode] == [0, 1, 2, 3, 4, 5, 130]


def test_unknown_has_its_own_symbol_and_value():
    assert Status.UNKNOWN.symbol == "?"
    assert json.loads(json.dumps({"s": Status.UNKNOWN})) == {"s": "unknown"}


def test_a_crashing_probe_is_unknown_not_skip(registry):
    _register(
        registry,
        _probe("a"),
        _probe("b", raises=AttributeError("'NoneType' object has no attribute 'x'")),
    )
    diag = Engine().run()
    crashed = diag.results_by_name()["b"]
    assert crashed.status == Status.UNKNOWN
    assert "AttributeError" in crashed.summary
    assert diag.status == Status.UNKNOWN
    assert diag.status.exit_code == 3


def test_a_failure_outranks_a_crash(registry):
    _register(
        registry,
        _probe("a", Status.FAIL),
        _probe("b", raises=RuntimeError("boom")),
    )
    assert Engine().run().status == Status.FAIL


def test_nothing_measured_is_unknown_not_passed(registry):
    _register(registry, _probe("a", Status.SKIP), _probe("b", Status.SKIP))
    diag = Engine().run()
    assert diag.status == Status.UNKNOWN
    assert "all checks passed" not in render_terminal(diag, color=False)
    assert json.loads(render_json(diag))["status"] == "unknown"


def test_unknown_result_renders_in_every_format(registry):
    _register(registry, _probe("a"), _probe("b", raises=RuntimeError("boom")))
    diag = Engine().run()
    text = render_terminal(diag, color=False, unicode=True)
    assert "? " in text and "boom" in text
    ascii_text = render_terminal(diag, color=False, unicode=False)
    assert "?? " in ascii_text
