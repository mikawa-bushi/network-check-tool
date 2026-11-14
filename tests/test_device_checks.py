"""`check` with devices: each verdict follows what was measured.

The network snapshot, the ping sweep and the ARP table are fakes, so each
field situation is exact. Every test is a case that used to give another
verdict or exit code.
"""

import pytest

from netdiag import netinfo
from netdiag import runner as runner_mod
from netdiag.core import CheckResult, Probe, Status, register
from netdiag.core.probe import _REGISTRY
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
from netdiag.report import render_check_text

MAC = "00:00:5e:00:53:27"


def _eth(name="eth0", ip="192.0.2.100", prefix=24, up=True, arp=True):
    return Interface(
        name=name,
        kind=KIND_ETHERNET,
        up=up,
        arp=arp,
        addresses=[Address(ip, prefix)],
        stats=LinkStats(rx_packets=10, tx_packets=10, rx_errors=0),
    )


def _ping(target, received, sent=10, error=None):
    return PingStats(
        target=target,
        address=target,
        sent=sent,
        received=received,
        rtts_ms=[0.4] * received,
        error=error,
    )


@pytest.fixture
def line(monkeypatch):
    """A PC on 192.0.2.0/24; tests set the replies and the ARP table."""
    state = {
        "hn": HostNetwork(interfaces=[_eth()], source="test"),
        "replies": {},
        "table": [],
        "lookups": {},
        "pinged": [],
    }
    monkeypatch.setattr(netinfo, "snapshot", lambda max_age=5.0: state["hn"])
    monkeypatch.setattr(netinfo, "link_stats", lambda: {})
    monkeypatch.setattr(netinfo, "neighbors", lambda: list(state["table"]))

    def neighbor(ip):
        seq = state["lookups"].get(ip)
        return seq.pop(0) if seq else None

    monkeypatch.setattr(netinfo, "neighbor", neighbor)

    def ping_many(targets, count=10, **kw):
        state["pinged"].extend(targets)
        return {t: state["replies"].get(t, _ping(t, 0)) for t in targets}

    monkeypatch.setattr(runner_mod, "ping_many", ping_many)
    monkeypatch.setattr(runner_mod.time, "sleep", lambda s: None)
    return state


def _check(devices, **kw):
    kw.setdefault("include_host", False)
    return runner_mod.run_check(devices, config={"internet": False}, **kw)


def _codes(report, i=0):
    return {r.name: r.code for r in report.devices[i].diagnosis.results}


# --------------------------------------------------------- verdict and exit


def test_a_device_that_could_not_be_pinged_makes_check_incomplete(line):
    line["replies"]["192.0.2.39"] = _ping("192.0.2.39", 0, sent=0, error="no ping")
    rep = _check([Device(name="PLC", host="192.0.2.39")])
    assert rep.devices[0].status == Status.UNKNOWN
    assert rep.exit_code == 3


def test_a_healthy_line_checked_without_the_host_exits_0(line):
    line["replies"]["192.0.2.39"] = _ping("192.0.2.39", 10)
    rep = _check([Device(name="PLC", host="192.0.2.39")])
    assert (rep.host_status, rep.status, rep.exit_code) == (Status.SKIP, Status.OK, 0)
    assert rep.to_dict()["host"] is None
    assert rep.to_dict()["devices"][0]["status"] == "ok"


@pytest.mark.parametrize("lang", ["en", "ja"])
def test_a_report_without_the_host_never_claims_this_pc_was_checked(line, lang):
    from netdiag.i18n import set_language
    from netdiag.report import render_check_html

    set_language(lang)
    line["replies"]["192.0.2.39"] = _ping("192.0.2.39", 10)
    rep = _check([Device(name="PLC", host="192.0.2.39")])
    text = render_check_text(rep, color=False)
    html = render_check_html(rep)
    for this_pc in ("This PC", "this PC", "このPC"):
        assert this_pc not in text and this_pc not in html
    assert "1 device(s)" in text or "機器 1台" in text


def test_nic_errors_appear_once_and_follow_only_and_skip(line, monkeypatch):
    grown = {"eth0": LinkStats(rx_packets=20, tx_packets=20, rx_errors=4)}
    monkeypatch.setattr(netinfo, "link_stats", lambda: grown)

    def rows(**kw):
        rep = runner_mod.run_check([], config={"internet": False}, **kw)
        return [r.code for r in rep.host.results if r.name == "link_errors"]

    assert rows(only=["link_errors"]) == ["link_errors.increased"]
    assert rows(only=["interface", "link_errors"]) == ["link_errors.increased"]
    assert rows(only=["interface"]) == []
    assert rows(only=["interface", "link_errors"], skip=["link_errors"]) == []


def test_reasoning_is_recorded_for_the_report(line):
    rep = runner_mod.run_check([], config={"internet": False}, only=["interface"])
    assert rep.host.reasoning
    text = render_check_text(rep, color=False, verbose=True)
    assert "Reasoning:" in text


def test_host_checks_keep_their_own_ping_count(line):
    seen = {}

    @register
    class _CountSpy(Probe):
        name = "count_spy"

        def check(self, ctx):
            seen["count"] = ctx.get("count")
            return CheckResult.ok(self.name, "ok")

    try:
        rep = runner_mod.run_check([], config={}, only=["count_spy"])
    finally:
        _REGISTRY.pop("count_spy")
    assert seen["count"] is None
    assert rep.settings["count"] >= 1


# ------------------------------------------------------------------- route


def _with_default_gateway(line, *extra_routes):
    line["hn"].routes = [
        Route("0.0.0.0/0", gateway="192.0.2.1", interface="eth0"),
        *extra_routes,
    ]


def test_a_silent_device_only_the_default_gateway_leads_to_is_a_subnet_mismatch(
    line,
):
    _with_default_gateway(line)
    rep = _check([Device(name="PLC", host="198.51.100.39")])
    diag = rep.devices[0].diagnosis
    assert diag.primary.code == "route.default_only"
    assert "reach" in diag.primary.consequences
    assert "subnet mismatch" in diag.results_by_name()["route"].summary
    assert any("198.51.100.x/24" in s for s in diag.primary.runbook)


def test_a_routed_device_that_answers_keeps_its_route(line):
    _with_default_gateway(line)
    line["replies"]["198.51.100.39"] = _ping("198.51.100.39", 10)
    rep = _check([Device(name="PLC", host="198.51.100.39")])
    assert _codes(rep)["route"] == "route.via_default"
    assert rep.devices[0].status == Status.OK


def test_a_device_behind_a_specific_route_is_not_blamed_on_the_subnet(line):
    _with_default_gateway(
        line, Route("198.51.100.0/24", gateway="192.0.2.254", interface="eth0")
    )
    rep = _check([Device(name="PLC", host="198.51.100.39")])
    assert _codes(rep)["route"] == "route.via_gateway"
    assert rep.devices[0].diagnosis.primary.code == "reach.no_reply"


@pytest.mark.parametrize(
    "other, ambiguous",
    [
        (_eth("eth1", "192.0.2.101", up=False), False),  # unplugged NIC
        (_eth("eth1", "192.0.0.5", prefix=16), False),  # wider overlapping subnet
        (_eth("eth1", "192.0.2.101"), True),  # the same subnet twice
    ],
)
def test_only_an_equal_up_subnet_makes_the_outgoing_nic_ambiguous(
    line, other, ambiguous
):
    line["hn"].interfaces.append(other)
    line["replies"]["192.0.2.39"] = _ping("192.0.2.39", 10)
    rep = _check([Device(name="PLC", host="192.0.2.39")])
    assert (_codes(rep)["route"] == "route.ambiguous") is ambiguous


# --------------------------------------------------------------- ARP evidence


def _arp_entry(line, confirmed_age):
    entry = Neighbor(
        "192.0.2.39", mac=MAC, state="reachable", confirmed_age=confirmed_age
    )
    line["table"] = [entry]
    line["lookups"]["192.0.2.39"] = [entry]


def test_an_arp_answer_during_the_test_shows_a_silent_device_is_present(line):
    _arp_entry(line, confirmed_age=0.5)
    rep = _check([Device(name="HMI", host="192.0.2.39")])
    assert _codes(rep)["reach"] == "reach.icmp_blocked"
    assert rep.devices[0].status == Status.WARN


@pytest.mark.parametrize("died_ago", [5.0, 20.0, 40.0])
@pytest.mark.parametrize(
    "ports, codes",
    [
        ([], {"reach": "reach.no_reply"}),
        ([502], {"reach": "reach.no_icmp", "ports": "ports.no_response"}),
    ],
)
def test_a_device_that_just_lost_power_is_not_called_present(
    line, monkeypatch, died_ago, ports, codes
):
    _arp_entry(line, confirmed_age=died_ago)
    monkeypatch.setattr(
        device_probes,
        "tcp_check_many",
        lambda pairs, **kw: {
            (h, p): PortCheck(host=h, port=p, attempts=2, state="filtered")
            for h, p in pairs
        },
    )
    rep = _check([Device(name="PLC", host="192.0.2.39", ports=ports)])
    assert {k: v for k, v in _codes(rep).items() if k != "route"} == codes
    assert (rep.devices[0].status, rep.exit_code) == (Status.FAIL, 1)


def test_an_arp_entry_being_verified_is_waited_for(line):
    line["lookups"]["192.0.2.39"] = [
        Neighbor("192.0.2.39", mac=MAC, state="delay", confirmed_age=25),
        Neighbor("192.0.2.39", mac=MAC, state="probe", confirmed_age=30),
        Neighbor("192.0.2.39", mac=MAC, state="reachable", confirmed_age=0),
    ]
    rep = _check([Device(name="HMI", host="192.0.2.39")])
    assert _codes(rep)["reach"] == "reach.icmp_blocked"


def test_a_silent_device_behind_a_tunnel_is_not_called_powered_off(line):
    wg = _eth("wg0", "10.99.0.2", prefix=32, arp=False)
    line["hn"].interfaces.append(wg)
    line["hn"].routes = [Route("10.50.0.0/16", gateway=None, interface="wg0")]
    rep = _check([Device(name="PLC", host="10.50.1.10")])
    reach = rep.devices[0].diagnosis.results_by_name()["reach"]
    assert reach.code == "reach.no_reply"
    assert "ARP" not in reach.summary
    assert reach.detail["arp"] is False


def test_probes_use_the_sweep_and_never_ping_on_their_own(line, monkeypatch):
    def no_second_ping(*a, **kw):
        raise AssertionError("a device probe pinged on its own")

    monkeypatch.setattr(device_probes, "ping", no_second_ping)
    line["replies"]["127.0.0.1"] = _ping("127.0.0.1", 10)
    rep = _check([Device(name="self", host="127.0.0.1")])
    assert line["pinged"] == ["127.0.0.1"]
    assert _codes(rep)["reach"] == "reach.ok"
