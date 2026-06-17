"""Round-2 review fixes (2026-06-17): ping-probe consolidation.

* M9  -- ``classify_ping`` owns the shared loss-threshold cascade and metric/
  detail assembly; the warn/ok messages are evaluated lazily so the failure
  branch never needs an RTT.
* M10 -- ``icmp.ping`` gained a ``family`` flag; ``dualstack`` delegates to it
  (one parser, no brittle substring match) and propagates ``count``.
"""

from netdiag.core import Context, Status
from netdiag.probes import dualstack, icmp
from netdiag.probes.icmp import classify_ping

# ------------------------------------------------------------------ M9


def test_classify_ping_ok_records_metrics_and_detail():
    r = classify_ping(
        "p",
        12.3,
        0.0,
        "raw",
        fail_msg="f",
        warn_msg=lambda: "w",
        ok_msg=lambda: "ok 12.3",
    )
    assert r.status is Status.OK
    assert r.summary == "ok 12.3"
    assert r.metrics["loss_pct"] == 0.0
    assert r.metrics["rtt_ms"] == 12.3
    assert r.detail["output"] == "raw"


def test_classify_ping_warns_on_partial_loss():
    r = classify_ping(
        "p",
        12.3,
        25.0,
        "raw",
        fail_msg="f",
        warn_msg=lambda: "w",
        ok_msg=lambda: "ok",
    )
    assert r.status is Status.WARN
    assert r.summary == "w"


def test_classify_ping_fail_is_lazy_and_omits_missing_rtt():
    # rtt is None on total loss; the warn/ok messages must NOT be evaluated
    # (this one would raise), proving only the chosen branch runs.
    def boom():
        raise AssertionError("warn/ok message evaluated on the fail path")

    r = classify_ping(
        "p", None, 100.0, "raw", fail_msg="down", warn_msg=boom, ok_msg=boom
    )
    assert r.status is Status.FAIL
    assert r.summary == "down"
    assert "rtt_ms" not in r.metrics  # None RTT is not recorded


def test_classify_ping_merges_extra_metrics_and_detail():
    r = classify_ping(
        "gw",
        5.0,
        0.0,
        "raw",
        fail_msg="f",
        warn_msg=lambda: "w",
        ok_msg=lambda: "ok",
        extra_metrics={"has_gateway": 1.0},
        extra_detail={"gateway": "10.0.0.1"},
    )
    assert r.metrics["has_gateway"] == 1.0
    assert r.detail["gateway"] == "10.0.0.1"


# ------------------------------------------------------------------ M10: ping family


class _FakeRun:
    def __init__(self, args):
        self.args = args

    stdout = "rtt min/avg/max = 1.0/2.0/3.0 ms\n0% packet loss"
    stderr = ""


def test_ping_injects_family_flag(monkeypatch):
    captured = {}

    def fake_run(args, timeout=None):
        captured["args"] = args
        return _FakeRun(args)

    monkeypatch.setattr(icmp, "run", fake_run)
    monkeypatch.setattr(icmp, "is_windows", lambda: False)

    icmp.ping("h", count=2, family=6)
    assert "-6" in captured["args"]
    icmp.ping("h", count=2, family=4)
    assert "-4" in captured["args"]
    icmp.ping("h", count=2)
    assert "-4" not in captured["args"] and "-6" not in captured["args"]


# ------------------------------------------------------------------ M10: dualstack


def test_ping_family_reachable_unreachable_and_propagates_count(monkeypatch):
    monkeypatch.setattr(dualstack, "have", lambda cmd: True)
    calls = {}

    def fake_ping(host, count=2, family=None):
        calls["family"], calls["count"] = family, count
        if family == 4:
            return (10.0, 0.0, "0% packet loss")
        return (None, 100.0, "100% packet loss")

    monkeypatch.setattr(dualstack, "ping", fake_ping)

    assert dualstack._ping_family("h", v6=False, count=3) is True
    assert calls == {"family": 4, "count": 3}
    assert dualstack._ping_family("h", v6=True) is False


def test_ping_family_none_without_ping_or_output(monkeypatch):
    monkeypatch.setattr(dualstack, "have", lambda cmd: False)
    assert dualstack._ping_family("h", v6=False) is None

    monkeypatch.setattr(dualstack, "have", lambda cmd: True)
    monkeypatch.setattr(
        dualstack, "ping", lambda host, count=2, family=None: (None, 100.0, "")
    )
    assert dualstack._ping_family("h", v6=False) is None


def test_dualstack_v4_ok_v6_down_warns(monkeypatch):
    monkeypatch.setattr(dualstack, "_has_family", lambda host, fam: True)

    def fake_pf(host, v6, count=2):
        return False if v6 else True

    monkeypatch.setattr(dualstack, "_ping_family", fake_pf)
    res = dualstack.DualStackProbe().check(Context({"dns_name": "h"}))
    assert res.status is Status.WARN
    assert "IPv6 unreachable" in res.summary
    assert res.detail["ipv4_ok"] is True and res.detail["ipv6_ok"] is False


def test_dualstack_check_propagates_count(monkeypatch):
    monkeypatch.setattr(dualstack, "_has_family", lambda host, fam: True)
    seen = []

    def fake_pf(host, v6, count=2):
        seen.append(count)
        return True

    monkeypatch.setattr(dualstack, "_ping_family", fake_pf)
    dualstack.DualStackProbe().check(Context({"dns_name": "h", "count": 5}))
    assert seen == [5, 5]
