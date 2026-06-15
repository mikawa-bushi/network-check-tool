"""Tests for issue #29 (M10 phase 3): report renderers.

Covers ``netdiag.report``'s terminal / JSON / HTML renderers by building a real
``Diagnosis`` from synthetic probes and asserting the rendered output.
"""

import json

import pytest

from netdiag.core import CheckResult, Status, Probe
from netdiag.core.probe import _REGISTRY
from netdiag.diagnosis import Engine
from netdiag.report import render_html, render_json, render_terminal


@pytest.fixture
def isolated_registry():
    saved = dict(_REGISTRY)
    _REGISTRY.clear()
    yield _REGISTRY
    _REGISTRY.clear()
    _REGISTRY.update(saved)


def _make_probe(pname, player, pdeps, status, summary=None):
    text = summary if summary is not None else f"{pname} -> {status.value}"

    class _P(Probe):
        name = pname
        title = pname
        layer = player
        depends_on = pdeps

        def check(self, ctx):
            return CheckResult(name=self.name, status=status, summary=text)

    _P.__name__ = f"Probe_{pname}"
    return _P


def _register(reg, *probes):
    for p in probes:
        reg[p.name] = p


def _failing_diag(reg, verbose=False):
    # One failing root cause ("a") with a healthy peer ("b").
    _register(
        reg,
        _make_probe("a", "L2", [], Status.FAIL),
        _make_probe("b", "L3", [], Status.OK),
    )
    return Engine(verbose=verbose).run()


# ------------------------------------------------------------------------- JSON


def test_render_json_failing_roundtrips(isolated_registry):
    diag = _failing_diag(isolated_registry)
    obj = json.loads(render_json(diag))

    assert obj["healthy"] is False
    assert {r["name"] for r in obj["results"]} == {"a", "b"}
    assert len(obj["hypotheses"]) >= 1
    h = obj["hypotheses"][0]
    assert h["cause"] == "a"
    for key in (
        "cause",
        "confidence",
        "summary",
        "evidence",
        "consequences",
        "runbook",
    ):
        assert key in h


def test_render_json_healthy(isolated_registry):
    _register(isolated_registry, _make_probe("a", "L2", [], Status.OK))
    obj = json.loads(render_json(Engine().run()))

    assert obj["healthy"] is True
    assert obj["hypotheses"] == []


# --------------------------------------------------------------------- terminal


def test_render_terminal_lists_probes_and_cause(isolated_registry):
    diag = _failing_diag(isolated_registry)
    out = render_terminal(diag, color=False)

    # Each probe's row carries its summary ("<name> -> <status>").
    assert "a -> fail" in out
    assert "b -> ok" in out
    assert "診断 (Diagnosis)" in out
    assert "推定原因: a" in out


def test_render_terminal_color_toggle(isolated_registry):
    diag = _failing_diag(isolated_registry)

    assert "\033[" not in render_terminal(diag, color=False)
    assert "\033[" in render_terminal(diag, color=True)


def test_render_terminal_verbose_adds_reasoning(isolated_registry):
    # Two-sided: the Engine must run with verbose=True to populate diag.reasoning,
    # AND render_terminal must be called with verbose=True to emit the section.
    diag = _failing_diag(isolated_registry, verbose=True)
    out = render_terminal(diag, color=False, verbose=True)

    assert "推論過程" in out


def test_render_terminal_failures_without_hypotheses():
    # Not reachable via Engine (failures always yield hypotheses), but Diagnosis is
    # public API — pin the "failures but no hypotheses" branch via direct construction.
    from netdiag.diagnosis import Diagnosis

    diag = Diagnosis(
        results=[CheckResult(name="x", status=Status.WARN, summary="w")],
        hypotheses=[],
        healthy=False,
    )
    out = render_terminal(diag, color=False)

    assert "失敗はありませんが警告があります" in out


def test_render_terminal_healthy_verdict(isolated_registry):
    _register(isolated_registry, _make_probe("a", "L2", [], Status.OK))
    out = render_terminal(Engine().run(), color=False)

    assert "All checks passed" in out


# ------------------------------------------------------------------------- HTML


def test_render_html_is_self_contained(isolated_registry):
    diag = _failing_diag(isolated_registry)
    out = render_html(diag)

    assert out.lstrip().startswith("<!doctype html>")
    # No external resources / scripts — must work fully offline.
    assert "http://" not in out and "https://" not in out
    assert "<script" not in out
    # Content present: the verdict and a result-table cell for probe "a".
    assert "suspected cause" in out
    assert "<td>a</td>" in out


def test_render_html_escapes_user_text(isolated_registry):
    _register(
        isolated_registry,
        _make_probe("a", "L2", [], Status.FAIL, summary="<script>alert(1)</script>"),
    )
    out = render_html(Engine().run())

    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out
