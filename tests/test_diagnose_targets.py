"""netdiag.runner.diagnose_targets: one check explains several degraded hosts."""

from types import SimpleNamespace as NS

import netdiag.runner as runner
from netdiag.diagnosis import Hypothesis


def _report(host_hyps, devices, host_primary=None):
    return NS(
        host=NS(hypotheses=host_hyps, primary=host_primary),
        devices=[
            NS(device=NS(host=h), diagnosis=NS(primary=p) if p else None)
            for h, p in devices
        ],
    )


def test_each_host_gets_its_own_cause_from_one_check(monkeypatch):
    calls = []

    def fake_run_check(devices, config=None, only=None):
        calls.append([d.host for d in devices])
        return _report([], [("a", "arp"), ("b", None)], host_primary="ok")

    monkeypatch.setattr(runner, "run_check", fake_run_check)

    found = runner.diagnose_targets(["a", "b", "a"])

    assert calls == [["a", "b"]]  # one run, duplicates dropped
    assert found == {"a": "arp", "b": "ok"}


def test_a_fault_on_this_pc_explains_every_host(monkeypatch):
    link_down = Hypothesis(cause="link", confidence="high", summary="link down")
    monkeypatch.setattr(
        runner,
        "run_check",
        lambda devices, config=None, only=None: _report(
            [link_down], [("a", "arp"), ("b", None)]
        ),
    )
    assert runner.diagnose_targets(["a", "b"]) == {"a": link_down, "b": link_down}


def test_no_hosts_runs_nothing(monkeypatch):
    monkeypatch.setattr(runner, "run_check", lambda *a, **k: 1 / 0)
    assert runner.diagnose_targets([]) == {}
