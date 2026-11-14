"""Per-device diagnosis: route → reach → ports, and the full runner.

A fake network snapshot and injected measurements stand in for the real
network, so each field situation is reproduced exactly:

* the PC is in another subnet than the PLC (most common field mistake),
* the device is gone (no ARP answer) vs alive but ignoring ping,
* duplicate replies / unexpected MAC (IP conflict, replaced device),
* port closed (alive, nothing listening) vs no answer at all.
"""

from contextlib import contextmanager
from unittest import mock

import pytest

from netdiag import netinfo
from netdiag.core import CheckResult, Probe, Status
from netdiag.core.probe import _REGISTRY
from netdiag.diagnosis import Engine
from netdiag.inventory import Device
from netdiag.measure import PingStats
from netdiag.measure.tcp import PortCheck
from netdiag.netinfo.model import (
    KIND_ETHERNET,
    Address,
    HostNetwork,
    Interface,
    LinkStats,
    Neighbor,
    Route,
)
from netdiag.probes import device as device_probes
from netdiag.probes.device import DeviceEvidence, l2_evidence

DEVICE = ["route", "reach", "ports"]


def _hn(gateway=None, eth_up=True):
    eth = Interface(
        name="イーサネット",
        kind=KIND_ETHERNET,
        up=eth_up,
        addresses=[Address("192.168.3.100", 24)],
        stats=LinkStats(rx_packets=10, tx_packets=10, rx_errors=0),
    )
    routes = (
        [Route("0.0.0.0/0", gateway=gateway, interface="イーサネット")]
        if gateway
        else []
    )
    return HostNetwork(interfaces=[eth], routes=routes, source="test")


def _ping(received, sent=10, dups=0, rtt=0.4, error=None):
    return PingStats(
        target="x",
        sent=sent,
        received=received,
        duplicates=dups,
        rtts_ms=[rtt] * received,
        error=error,
    )


def _cfg(target, hn=None, ping=None, before=None, after=None):
    evidence = DeviceEvidence(
        address=target["host"],
        ping=ping,
        ping_window=3.0,
        neighbor_before=before,
        neighbor_after=after,
    )
    return {"target": target, "_netinfo": hn or _hn(), "evidence": evidence}


@contextmanager
def _answering_ports(ports):
    """The ports probe connects; *ports* maps a port to its PortCheck."""
    fake = lambda pairs, **kw: {(h, p): ports[p] for h, p in pairs}  # noqa: E731
    with mock.patch.object(device_probes, "tcp_check_many", fake):
        yield


def _run(target, hn=None, ping=None, before=None, after=None, ports=None):
    with _answering_ports(ports or {}):
        return Engine(config=_cfg(target, hn, ping, before, after)).run(only=DEVICE)


def _tgt(host="192.168.3.39", ports=(), **kw):
    return {"name": "PLC-1", "host": host, "ports": list(ports), **kw}


# ------------------------------------------------------------------ route


def test_other_subnet_without_gateway_is_the_root_cause():
    diag = _run(_tgt("192.168.1.39"), ping=_ping(0))
    assert diag.primary.cause == "route"
    assert diag.primary.code == "route.no_route"
    assert "reach" in diag.primary.consequences
    steps = " ".join(diag.primary.runbook)
    assert "192.168.1.x/24" in steps  # tells the engineer which subnet to use


def test_link_down_nic_and_special_addresses():
    assert (
        _run(_tgt(), hn=_hn(eth_up=False), ping=_ping(0)).primary.code
        == "route.link_down"
    )
    assert (
        _run(_tgt("192.168.3.255"), ping=_ping(0)).primary.code
        == "route.special_address"
    )
    self_ip = _run(_tgt("192.168.3.100"), ping=_ping(10))
    assert self_ip.results_by_name()["route"].code == "route.self"


def test_routed_target_via_gateway_is_fine():
    diag = _run(_tgt("10.9.0.5"), hn=_hn(gateway="192.168.3.1"), ping=_ping(10))
    assert diag.healthy
    assert diag.results_by_name()["route"].code == "route.via_default"
    assert diag.results_by_name()["route"].status == Status.OK


# ------------------------------------------------------------------ reach


def test_device_absent_no_arp_answer():
    after = Neighbor(ip="192.168.3.39", state="failed")
    diag = _run(_tgt(ports=[502]), ping=_ping(0), after=after)
    assert diag.primary.code == "reach.no_arp"
    assert diag.primary.confidence == "high"
    assert (
        diag.results_by_name()["ports"].status == Status.SKIP
    )  # consequence, not a 2nd cause


def test_alive_device_that_ignores_ping_is_only_a_warning():
    before = Neighbor(ip="192.168.3.39", state="incomplete")
    after = Neighbor(ip="192.168.3.39", mac="00:26:92:aa:bb:cc", state="reachable")
    diag = _run(_tgt(), ping=_ping(0), before=before, after=after)
    assert not diag.healthy
    assert diag.primary.code == "reach.icmp_blocked"
    assert diag.primary.confidence == "low"


def test_duplicate_replies_mean_ip_conflict():
    diag = _run(_tgt(), ping=_ping(10, dups=3))
    assert diag.primary.code == "reach.duplicate_reply"


def test_unexpected_mac_is_flagged():
    after = Neighbor(
        ip="192.168.3.39", mac="aa:bb:cc:00:00:01", state="reachable", confirmed_age=0
    )
    diag = _run(_tgt(mac="00-26-92-AA-BB-CC"), ping=_ping(10), after=after)
    assert diag.primary.code == "reach.mac_mismatch"


def test_intermittent_loss_and_custom_rtt_limit():
    assert _run(_tgt(), ping=_ping(9)).primary.code == "reach.loss"
    slow = _run(_tgt(rtt_warn_ms=2), ping=_ping(10, rtt=5.0))
    assert slow.primary.code == "reach.latency"
    ok = _run(
        _tgt(loss_warn_pct=20), ping=_ping(9)
    )  # 10 % loss within the device's tolerance
    assert ok.healthy


def test_unmeasurable_ping_is_not_a_device_fault():
    diag = _run(_tgt(), ping=_ping(0, error="ping command not available"))
    assert diag.results_by_name()["reach"].status == Status.UNKNOWN
    assert diag.primary is None or diag.primary.cause != "reach"


@pytest.mark.parametrize(
    "before, after, expected",
    [
        (
            None,
            Neighbor("ip", mac="00:11:22:33:44:55", state="stale"),
            "present",
        ),  # new entry
        (
            Neighbor("ip", mac="00:11:22:33:44:55", state="stale"),
            Neighbor("ip", mac="00:11:22:33:44:55", state="stale"),
            "unknown",  # an old cache entry proves nothing
        ),
        (
            None,
            Neighbor("ip", mac="00:11:22:33:44:55", state="reachable", confirmed_age=1),
            "present",
        ),
        (
            None,
            Neighbor(
                "ip", mac="00:11:22:33:44:55", state="reachable", confirmed_age=60
            ),
            "unknown",
        ),
        (None, Neighbor("ip", state="incomplete"), "absent"),
        (None, None, "unknown"),
    ],
)
def test_l2_evidence_requires_fresh_arp(before, after, expected):
    assert l2_evidence(before, after, window_s=3.0) == expected


# ------------------------------------------------------------------ ports


def _pc(port, state, ok=0, attempts=2):
    return PortCheck(
        host="192.168.3.39",
        port=port,
        attempts=attempts,
        state=state,
        times_ms=[1.0] * ok,
    )


def test_closed_port_on_a_live_device():
    diag = _run(_tgt(ports=[502]), ping=_ping(10), ports={502: _pc(502, "closed")})
    assert diag.primary.code == "ports.closed"
    assert any("502" in s for s in diag.primary.runbook)


def test_routed_device_silent_to_ping_and_tcp_is_no_response():
    hn = _hn(gateway="192.168.3.1")
    diag = _run(
        _tgt("10.9.0.5", ports=[502]),
        hn=hn,
        ping=_ping(0),
        ports={502: _pc(502, "filtered")},
    )
    assert diag.primary.code == "ports.no_response"  # never "reached but port closed"


def test_icmp_blocked_but_port_open_is_healthy_enough():
    hn = _hn(gateway="192.168.3.1")
    diag = _run(
        _tgt("10.9.0.5", ports=[44818]),
        hn=hn,
        ping=_ping(0),
        ports={44818: _pc(44818, "open", ok=2)},
    )
    codes = {r.name: r.code for r in diag.results}
    assert codes["ports"] == "ports.ok"
    assert codes["reach"] == "reach.no_icmp"  # a warning, not the verdict


def test_flaky_port_warns():
    diag = _run(_tgt(ports=[502]), ping=_ping(10), ports={502: _pc(502, "open", ok=1)})
    assert diag.primary.code == "ports.flaky"


# --------------------------------------------------------- engine semantics


@pytest.fixture
def isolated_registry():
    saved = dict(_REGISTRY)
    _REGISTRY.clear()
    yield _REGISTRY
    _REGISTRY.clear()
    _REGISTRY.update(saved)


def _make(name, deps, status, scope="host", default=True, internet=False):
    class _P(Probe):
        pass

    _P.name, _P.depends_on, _P.layer = name, deps, "L3"
    _P.scope, _P.default, _P.requires_internet = scope, default, internet
    _P.check = lambda self, ctx: CheckResult(name=name, status=status, summary=name)
    return _P


def test_transitive_consequences_are_not_run(isolated_registry):
    # a fails -> b is skipped as a consequence -> c (depends on b) must be too.
    for p in (
        _make("a", [], Status.FAIL),
        _make("b", ["a"], Status.FAIL),
        _make("c", ["b"], Status.FAIL),
    ):
        isolated_registry[p.name] = p
    diag = Engine().run()
    assert [h.cause for h in diag.hypotheses] == ["a"]
    by = diag.results_by_name()
    assert by["c"].status == Status.SKIP and by["c"].code == "c.not_evaluated"
    assert set(diag.primary.consequences) == {"b", "c"}


def test_default_selection_honours_scope_default_and_internet(isolated_registry):
    for p in (
        _make("host", [], Status.OK),
        _make("dev", [], Status.OK, scope="target"),
        _make("optin", [], Status.OK, default=False),
        _make("wan", [], Status.OK, internet=True),
    ):
        isolated_registry[p.name] = p
    assert Engine(config={"internet": False}).select() == ["host"]
    assert sorted(Engine(config={"internet": True}).select()) == ["host", "wan"]
    with_device = Engine(config={"target": {"host": "192.0.2.1"}})
    assert with_device.select(only=["dev", "optin"]) == ["dev", "optin"]


# ------------------------------------------------------------------ runner


def test_run_check_end_to_end_with_fakes(monkeypatch):
    from netdiag import runner

    hn = _hn()
    monkeypatch.setattr(netinfo, "snapshot", lambda max_age=5.0: hn)
    monkeypatch.setattr(
        netinfo,
        "link_stats",
        lambda: {"イーサネット": LinkStats(rx_packets=20, tx_packets=20, rx_errors=4)},
    )
    monkeypatch.setattr(
        netinfo,
        "neighbors",
        lambda: [
            Neighbor(
                "192.168.3.40",
                mac="00:26:92:00:00:40",
                state="reachable",
                confirmed_age=0,
            )
        ],
    )
    asked = []

    def fake_neighbor(ip):
        asked.append(ip)
        return None  # per-device lookup inconclusive: the table read stands

    monkeypatch.setattr(netinfo, "neighbor", fake_neighbor)
    calls = {}

    def fake_ping_many(targets, count, interval, timeout, **kw):
        calls["targets"] = list(targets)
        return {
            "192.168.3.39": _ping(10),
            "192.168.3.40": _ping(0),
            "192.168.3.41": _ping(0),
        }

    monkeypatch.setattr(runner, "ping_many", fake_ping_many)
    devices = [
        Device(name="PLC-1", host="192.168.3.39"),
        Device(name="HMI", host="192.168.3.40"),
        Device(name="ROBOT", host="192.168.3.41"),
        Device(name="OTHER", host="192.168.9.9"),
    ]
    rep = runner.run_check(
        devices, config={"internet": False}, only=["interface", "link_errors"]
    )
    # only routable targets are pinged, all in one sweep
    assert calls["targets"] == ["192.168.3.39", "192.168.3.40", "192.168.3.41"]
    status = {d.device.name: d.status for d in rep.devices}
    assert status == {
        "PLC-1": Status.OK,
        "HMI": Status.WARN,  # fresh ARP answer: present, ignores ping
        "ROBOT": Status.FAIL,  # no ARP entry after pinging an on-link IP
        "OTHER": Status.FAIL,  # other subnet, no gateway
    }
    # NIC errors grew during the run (0 -> 4): reported on the host side
    host_codes = {r.name: r.code for r in rep.host.results}
    assert host_codes["link_errors"] == "link_errors.increased"
    assert rep.exit_code == 1
    d = rep.to_dict()
    assert d["device_counts"] == {
        "ok": 1,
        "warn": 1,
        "fail": 2,
        "unknown": 0,
        "skip": 0,
    }


def test_run_check_skips_devices_when_the_pc_has_no_network(monkeypatch):
    from netdiag import runner

    hn = HostNetwork(interfaces=[], routes=[])
    monkeypatch.setattr(netinfo, "snapshot", lambda max_age=5.0: hn)
    monkeypatch.setattr(netinfo, "link_stats", lambda: {})
    monkeypatch.setattr(netinfo, "neighbors", lambda: [])

    def boom(*a, **k):
        raise AssertionError("must not ping when this PC has no usable interface")

    monkeypatch.setattr(runner, "ping_many", boom)
    monkeypatch.setattr("netdiag.probes.interface._primary_ip", lambda: None)
    rep = runner.run_check(
        [Device(name="PLC", host="192.168.3.39")], config={}, only=["interface"]
    )
    assert rep.devices[0].status == Status.SKIP
    assert rep.devices[0].skipped_reason
    assert rep.exit_code == 1


def test_ports_ok_settles_an_icmp_silent_device():
    from netdiag.runner import reconcile_device

    hn = _hn(gateway="192.168.3.1")
    engine = Engine(config=_cfg(_tgt("10.9.0.5", ports=[44818]), hn, _ping(0)))
    with _answering_ports({44818: _pc(44818, "open", ok=2)}):
        diag = reconcile_device(engine, engine.run(only=DEVICE))
    assert diag.healthy
    assert diag.results_by_name()["reach"].code == "reach.icmp_silent_ports_ok"


def test_silent_on_link_devices_get_a_direct_arp_answer(monkeypatch):
    # Windows: SendARP says "nobody answered" -> definite absence; a device in
    # the cache before the test that SendARP still resolves stays "unknown".
    from netdiag import runner

    hn = _hn()
    monkeypatch.setattr(netinfo, "snapshot", lambda max_age=5.0: hn)
    monkeypatch.setattr(netinfo, "link_stats", lambda: {})
    cached = Neighbor("192.168.3.40", mac="00:26:92:00:00:40", state="stale")
    monkeypatch.setattr(netinfo, "neighbors", lambda: [cached])
    answers = {
        "192.168.3.40": Neighbor(
            "192.168.3.40", mac="00:26:92:00:00:40", state="stale"
        ),
        "192.168.3.41": Neighbor("192.168.3.41", state="failed"),
    }
    asked = []

    def fake_neighbor(ip):
        asked.append(ip)
        return answers.get(ip)

    monkeypatch.setattr(netinfo, "neighbor", fake_neighbor)
    monkeypatch.setattr(
        runner,
        "ping_many",
        lambda targets, **kw: {
            "192.168.3.39": _ping(10),
            "192.168.3.40": _ping(0),
            "192.168.3.41": _ping(0),
        },
    )
    devices = [
        Device(name="OK", host="192.168.3.39"),
        Device(name="CACHED", host="192.168.3.40"),
        Device(name="GONE", host="192.168.3.41"),
    ]
    rep = runner.run_check(devices, config={"internet": False}, only=["interface"])
    assert sorted(asked) == ["192.168.3.40", "192.168.3.41"]  # only silent devices
    codes = {
        d.device.name: d.diagnosis.results_by_name()["reach"].code for d in rep.devices
    }
    assert codes["GONE"] == "reach.no_arp"
    assert codes["CACHED"] == "reach.no_reply"  # an old cache entry proves nothing


def _fake_dns(monkeypatch):
    import socket

    def getaddrinfo(host, port, family=0, *a, **k):
        if host == "plc.line1":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.3.39", 0))]
        raise socket.gaierror("no such name")

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def test_devices_given_by_name_are_pinged_at_their_resolved_address(monkeypatch):
    from netdiag import runner

    hn = _hn()
    monkeypatch.setattr(netinfo, "snapshot", lambda max_age=5.0: hn)
    monkeypatch.setattr(netinfo, "link_stats", lambda: {})
    monkeypatch.setattr(netinfo, "neighbors", lambda: [])
    monkeypatch.setattr(netinfo, "neighbor", lambda ip: None)
    _fake_dns(monkeypatch)
    pinged = []

    def fake_ping_many(targets, count, interval, timeout, **kw):
        pinged.extend(targets)
        return {t: _ping(10) for t in targets}

    monkeypatch.setattr(runner, "ping_many", fake_ping_many)
    devices = [
        Device(name="PLC", host="plc.line1"),
        Device(name="GONE", host="gone.line1"),
        Device(name="V6", host="fe80::1"),
        Device(name="LIT", host="192.168.3.40"),
    ]
    runner.run_check(devices, config={"internet": False}, only=["interface"])
    assert pinged == ["192.168.3.39", "fe80::1", "192.168.3.40"]
