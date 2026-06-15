"""Tests for issue #20 (P2): parallel execution of independent diagnostic probes.

The engine must run mutually-independent probes (those in the same dependency
tier) concurrently when ``jobs > 1``, while producing output identical to the
serial run: same ordered results, same reasoning, same skip-as-consequence
behaviour. Parallelism changes only wall-clock time, never the diagnosis.
"""

import time

import pytest

from netdiag.cli import cli
from netdiag.core import CheckResult, Status, Probe
from netdiag.core.probe import _REGISTRY
from netdiag.diagnosis import Engine
from netdiag.diagnosis.engine import _dependency_tiers


@pytest.fixture
def isolated_registry():
    """Swap the global registry for a clean one for the duration of a test."""
    saved = dict(_REGISTRY)
    _REGISTRY.clear()
    yield _REGISTRY
    _REGISTRY.clear()
    _REGISTRY.update(saved)


def _make_probe(pname, player, pdeps, status, sleep=0.0):
    class _P(Probe):
        name = pname
        title = pname
        layer = player
        depends_on = pdeps

        def check(self, ctx):
            if sleep:
                time.sleep(sleep)
            return CheckResult(
                name=self.name, status=status, summary=f"{self.name} -> {status.value}"
            )

    _P.__name__ = f"Probe_{pname}"
    return _P


def _register(reg, *probes):
    for p in probes:
        reg[p.name] = p


# --------------------------------------------------------------------- tiering


def test_dependency_tiers_group_independent_probes(isolated_registry):
    # a (no deps); b,c depend on a; d depends on b,c. The structural guarantee
    # behind parallelism: independent probes share a tier, dependents come later.
    _register(
        isolated_registry,
        _make_probe("a", "L2", [], Status.OK),
        _make_probe("b", "L3", ["a"], Status.OK),
        _make_probe("c", "L3", ["a"], Status.OK),
        _make_probe("d", "L7", ["b", "c"], Status.OK),
    )
    names = Engine().select()
    from netdiag.diagnosis.engine import _topo_order

    tiers = _dependency_tiers(_topo_order(names), names)
    assert tiers == [["a"], ["b", "c"], ["d"]]


# ---------------------------------------------------------------------- speedup


def test_independent_probes_run_concurrently(isolated_registry):
    # Timing is inherently noisy on shared CI; the structural guarantee lives in
    # test_dependency_tiers_group_independent_probes. Here we only assert that
    # parallel is meaningfully faster than serial, with a generous ratio so a
    # loaded runner doesn't cause spurious failures.
    sleep = 0.2
    _register(
        isolated_registry,
        _make_probe("a", "L2", [], Status.OK, sleep=sleep),
        _make_probe("b", "L2", [], Status.OK, sleep=sleep),
        _make_probe("c", "L2", [], Status.OK, sleep=sleep),
    )

    t0 = time.perf_counter()
    Engine().run(jobs=1)
    serial = time.perf_counter() - t0

    t0 = time.perf_counter()
    Engine().run(jobs=4)
    parallel = time.perf_counter() - t0

    # Three probes run at once instead of in series; even with scheduling
    # overhead the parallel run should beat serial by a wide margin.
    assert parallel < serial * 0.6


# -------------------------------------------------------------- identical output


def test_results_identical_serial_vs_parallel(isolated_registry):
    # a <- {b, c} <- d : b and c are an independent tier that can run in parallel.
    _register(
        isolated_registry,
        _make_probe("a", "L2", [], Status.OK),
        _make_probe("b", "L3", ["a"], Status.OK),
        _make_probe("c", "L3", ["a"], Status.WARN),
        _make_probe("d", "L7", ["b", "c"], Status.OK),
    )

    serial = Engine(verbose=True).run(jobs=1)
    parallel = Engine(verbose=True).run(jobs=4)

    assert [(r.name, r.status) for r in serial.results] == [
        (r.name, r.status) for r in parallel.results
    ]
    assert serial.reasoning == parallel.reasoning
    assert serial.healthy == parallel.healthy


# --------------------------------------------------------- dependencies respected


def test_dependency_result_visible_in_parallel(isolated_registry):
    # b depends on a; when b runs it must already see a's result in ctx, even
    # under parallel execution. If the assertion in check() fails, Probe.run
    # converts it to SKIP, so a PASS proves the dependency was visible.
    class A(Probe):
        name = "a"
        title = "a"
        layer = "L2"
        depends_on = []

        def check(self, ctx):
            return CheckResult(name="a", status=Status.OK, summary="a")

    class B(Probe):
        name = "b"
        title = "b"
        layer = "L3"
        depends_on = ["a"]

        def check(self, ctx):
            dep = ctx.result("a")
            assert dep is not None and dep.status == Status.OK
            return CheckResult(name="b", status=Status.OK, summary="b saw a")

    _register(isolated_registry, A, B)
    diag = Engine().run(jobs=4)
    assert diag.results_by_name()["b"].status == Status.OK


def test_failed_dependency_skips_in_parallel(isolated_registry):
    _register(
        isolated_registry,
        _make_probe("a", "L2", [], Status.FAIL),
        _make_probe("b", "L3", ["a"], Status.OK),
    )
    diag = Engine().run(jobs=4)
    b = diag.results_by_name()["b"]
    assert b.status == Status.SKIP
    assert "upstream a" in b.summary


def test_failed_dependency_skip_reasoning_identical_serial_vs_parallel(
    isolated_registry,
):
    # The skip-as-consequence reasoning string takes a different branch in
    # _evaluate_probe than the normal probe line; pin it across both paths.
    _register(
        isolated_registry,
        _make_probe("a", "L2", [], Status.FAIL),
        _make_probe("b", "L3", ["a"], Status.OK),
    )
    serial = Engine(verbose=True).run(jobs=1)
    parallel = Engine(verbose=True).run(jobs=4)
    assert serial.reasoning == parallel.reasoning
    assert any("skip b" in line for line in parallel.reasoning)


# ------------------------------------------------------------------- clamping


def test_jobs_zero_or_negative_behaves_serially(isolated_registry):
    _register(
        isolated_registry,
        _make_probe("a", "L2", [], Status.OK),
        _make_probe("b", "L3", ["a"], Status.OK),
    )
    for jobs in (0, -3):
        diag = Engine().run(jobs=jobs)
        names = [r.name for r in diag.results]
        assert names.index("a") < names.index("b")
        assert diag.healthy


# ------------------------------------------------------------------------- CLI


def test_check_cli_exposes_jobs_option():
    from click.testing import CliRunner

    result = CliRunner().invoke(cli, ["check", "--help"])
    assert result.exit_code == 0
    assert "--jobs" in result.output
