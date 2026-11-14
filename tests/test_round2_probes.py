"""Ping-based probe classification and the dual-stack probe.

* ``result_from_ping`` is the single OK/WARN/FAIL/SKIP cascade shared by the
  host ping probes (external / icmp): any reply with no loss is OK, partial loss
  or latency is WARN, no reply is FAIL, and "could not measure" is SKIP — never
  a false FAIL.
* ``dualstack`` pings over each family through the shared system-ping parser.
"""

from netdiag.core import Context, Status
from netdiag.measure import PingStats
from netdiag.probes import dualstack
from netdiag.probes.icmp import result_from_ping


def _stats(received, sent=10, rtts=None, error=None, dups=0):
    return PingStats(
        target="h",
        sent=sent,
        received=received,
        rtts_ms=rtts if rtts is not None else [0.5] * received,
        error=error,
        duplicates=dups,
    )


def test_all_replies_is_ok_with_statistics():
    r = result_from_ping("external", "h", _stats(10))
    assert r.status is Status.OK
    assert r.code == "external.ok"
    assert r.metrics["loss_pct"] == 0.0
    assert r.metrics["rtt_median_ms"] == 0.5
    assert "loss_upper_pct" in r.metrics  # the honest 95% bound is recorded


def test_partial_loss_warns():
    r = result_from_ping("external", "h", _stats(7))
    assert r.status is Status.WARN
    assert r.code == "external.degraded"


def test_no_reply_fails():
    r = result_from_ping("external", "h", _stats(0))
    assert r.status is Status.FAIL
    assert r.code == "external.unreachable"
    assert "rtt_ms" not in r.metrics


def test_measurement_error_is_unknown_not_fail():
    r = result_from_ping("external", "h", _stats(0, error="ping command not available"))
    assert r.status is Status.UNKNOWN


def test_slow_wan_reply_warns():
    r = result_from_ping("external", "h", _stats(10, rtts=[400.0] * 10))
    assert r.status is Status.WARN


# ------------------------------------------------------------------ dualstack


def test_ping_family_uses_system_ping_with_family_and_count(monkeypatch):
    calls = {}

    def fake_system_ping(host, count=2, family=None, **kw):
        calls["family"], calls["count"] = family, count
        st = PingStats(target=host, sent=count, raw="out")
        st.received = count if family == 4 else 0
        return st

    monkeypatch.setattr(dualstack, "system_ping", fake_system_ping)
    assert dualstack._ping_family("h", v6=False, count=3) is True
    assert calls == {"family": 4, "count": 3}
    assert dualstack._ping_family("h", v6=True) is False


def test_ping_family_none_when_ping_cannot_run(monkeypatch):
    monkeypatch.setattr(
        dualstack,
        "system_ping",
        lambda host, count=2, family=None: PingStats(
            target=host, error="ping command not available"
        ),
    )
    assert dualstack._ping_family("h", v6=False) is None


def test_dualstack_v4_ok_v6_down_warns(monkeypatch):
    monkeypatch.setattr(dualstack, "_has_family", lambda host, fam: True)

    def fake_pf(host, v6, count=2):
        return False if v6 else True

    monkeypatch.setattr(dualstack, "_family_ok", fake_pf)
    res = dualstack.DualStackProbe().check(Context({"dns_name": "h"}))
    assert res.status is Status.WARN
    assert "IPv6 unreachable" in res.summary
    assert res.detail["ipv4_ok"] is True and res.detail["ipv6_ok"] is False


def test_dualstack_sends_two_pings_whatever_the_count(monkeypatch):
    monkeypatch.setattr(dualstack, "_has_family", lambda host, fam: True)
    seen = []

    def fake_pf(host, v6, count=2):
        seen.append(count)
        return True

    monkeypatch.setattr(dualstack, "_family_ok", fake_pf)
    dualstack.DualStackProbe().check(Context({"dns_name": "h", "count": 5}))
    assert seen == [2, 2]


def test_ipv4_only_network_is_not_a_failure(monkeypatch):
    # IPv6 "Network is unreachable" on this PC = single-stack, not broken.
    monkeypatch.setattr(dualstack, "_has_family", lambda host, fam: True)
    monkeypatch.setattr(
        dualstack, "_family_ok", lambda host, v6, count=2: None if v6 else True
    )
    res = dualstack.DualStackProbe().check(Context({"dns_name": "h"}))
    assert res.status is Status.OK
    assert res.code == "dualstack.single_stack"


def test_tcp_second_opinion_when_ping_is_filtered(monkeypatch):
    monkeypatch.setattr(dualstack, "_ping_family", lambda host, v6, count=2: False)
    monkeypatch.setattr(dualstack, "_tcp_family", lambda host, family, **kw: True)
    assert dualstack._family_ok("h", v6=False, count=2) is True
    monkeypatch.setattr(dualstack, "_tcp_family", lambda host, family, **kw: False)
    assert dualstack._family_ok("h", v6=False, count=2) is False
    monkeypatch.setattr(dualstack, "_ping_family", lambda host, v6, count=2: None)
    monkeypatch.setattr(dualstack, "_tcp_family", lambda host, family, **kw: None)
    assert dualstack._family_ok("h", v6=True, count=2) is None
