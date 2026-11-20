"""Tests for issue #29 (M10 phase 3): report renderers.

Builds a real ``Diagnosis`` from synthetic probes and asserts what
``netdiag.report`` makes of it: the diagnosis data, and this PC's section of the
terminal and HTML check reports.
"""

import pytest

from netdiag.core import CheckResult, Probe, Status
from netdiag.core.probe import _REGISTRY
from netdiag.diagnosis import Engine
from netdiag.report import diagnosis_to_dict, render_check_html, render_check_text
from netdiag.runner import CheckReport


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


def _host_report(diag):
    return CheckReport(host=diag, hostname="pc", platform="test")


# ------------------------------------------------------------------------- data


def test_diagnosis_dict_failing(isolated_registry):
    diag = _failing_diag(isolated_registry)
    obj = diagnosis_to_dict(diag)

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


def test_diagnosis_dict_healthy(isolated_registry):
    _register(isolated_registry, _make_probe("a", "L2", [], Status.OK))
    obj = diagnosis_to_dict(Engine().run())

    assert obj["healthy"] is True
    assert obj["hypotheses"] == []


# --------------------------------------------------------------------- terminal


def test_render_check_text_lists_probes(isolated_registry):
    out = render_check_text(_host_report(_failing_diag(isolated_registry)), color=False)

    # Each probe's row carries its summary ("<name> -> <status>").
    assert "a -> fail" in out
    assert "b -> ok" in out


def test_render_check_text_color_toggle(isolated_registry):
    report = _host_report(_failing_diag(isolated_registry))

    assert "\033[" not in render_check_text(report, color=False)
    assert "\033[" in render_check_text(report, color=True)


def test_render_check_text_verbose_adds_reasoning(isolated_registry, ja):
    # Two-sided: the Engine must run with verbose=True to populate diag.reasoning,
    # AND render_check_text must be called with verbose=True to emit the section.
    report = _host_report(_failing_diag(isolated_registry, verbose=True))
    out = render_check_text(report, color=False, verbose=True)

    assert "推論過程" in out


# ------------------------------------------------------------------------- HTML


def test_render_check_html_is_self_contained(isolated_registry):
    out = render_check_html(_host_report(_failing_diag(isolated_registry)))

    assert out.lstrip().startswith("<!doctype html>")
    # No external resources / scripts — must work fully offline.
    assert "http://" not in out and "https://" not in out
    assert "<script" not in out
    # Content present: a result-table cell for probe "a".
    assert "<td>a</td>" in out


def test_render_check_html_escapes_user_text(isolated_registry):
    _register(
        isolated_registry,
        _make_probe("a", "L2", [], Status.FAIL, summary="<script>alert(1)</script>"),
    )
    out = render_check_html(_host_report(Engine().run()))

    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out
