from netdiag import netinfo
from netdiag.core import Context
from netdiag.measure import PingStats
from netdiag.netinfo.model import KIND_ETHERNET, Address, HostNetwork, Interface, Route
from netdiag.probes import gateway
from netdiag.probes.ipconfig import IpConfigProbe

NICS = [
    Interface(name=n, kind=KIND_ETHERNET, up=True, addresses=[Address(ip, 24)])
    for n, ip in (
        ("eth0", "192.168.1.10"),
        ("eth1", "10.0.0.10"),
        ("eth2", "172.16.0.10"),
    )
]
ROUTES = [
    Route("0.0.0.0/0", "10.0.0.1", "eth1", None),
    Route("0.0.0.0/0", "192.168.1.1", "eth0", 50),
    Route("0.0.0.0/0", "172.16.0.1", "eth2", 50),
]


def _ctx(routes):
    return Context({"_netinfo": HostNetwork(interfaces=NICS, routes=routes)})


def test_default_gateway_is_the_lowest_metric_first_listed_on_a_tie(monkeypatch):
    for routes, expected in (
        (ROUTES, "192.168.1.1"),
        (ROUTES[:1], "10.0.0.1"),
        ([], None),
    ):
        hn = HostNetwork(interfaces=NICS, routes=routes)
        monkeypatch.setattr(netinfo, "snapshot", lambda max_age=5.0, hn=hn: hn)
        assert gateway.default_gateway() == expected


def test_gateway_probe_pings_the_preferred_gateway(monkeypatch):
    pinged = []

    def fake_ping(target, **kw):
        pinged.append(target)
        return PingStats(
            target=target, address=target, sent=4, received=4, rtts_ms=[1.0] * 4
        )

    monkeypatch.setattr(gateway, "ping", fake_ping)
    res = gateway.GatewayProbe().check(_ctx(ROUTES))
    assert pinged == ["192.168.1.1"]
    assert (res.detail["gateway"], res.detail["interface"]) == ("192.168.1.1", "eth0")


def test_ip_settings_name_the_gateway_that_normally_wins():
    res = IpConfigProbe().check(_ctx(ROUTES))
    assert res.code == "ipconfig.multi_gateway"
    assert "192.168.1.1 normally wins" in res.summary
