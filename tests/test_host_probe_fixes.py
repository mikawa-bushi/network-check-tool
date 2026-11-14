"""This PC's checks give the verdict the network deserves.

The firewall rulesets here are reconstructed samples, not captures from
real machines.
"""

import pytest

from netdiag import netinfo
from netdiag.core import Context, ExitCode, Status
from netdiag.core.util import CommandResult
from netdiag.diagnosis.engine import Engine
from netdiag.measure import PingStats
from netdiag.measure.dns import DnsAnswer, DnsReplyError
from netdiag.netinfo.model import (
    KIND_ETHERNET,
    KIND_VIRTUAL,
    KIND_WIFI,
    Address,
    HostNetwork,
    Interface,
    Neighbor,
    Route,
)
from netdiag.probes import (
    dns,
    dualstack,
    firewall,
    gateway,
    icmp,
    neighbor,
    system,
    wifi,
)

GW = "192.0.2.1"
GW_MAC = "00:00:5e:00:53:01"


def nic(name, ip=None, prefix=24, up=True, kind=KIND_ETHERNET, speed=1000.0):
    return Interface(
        name=name,
        up=up,
        kind=kind,
        speed_mbps=speed,
        duplex="full",
        addresses=[Address(ip, prefix)] if ip else [],
    )


def line_pc(routes=None, resolvers=(), source="linux:ip-json"):
    return HostNetwork(
        interfaces=[nic("eth0", "192.0.2.10")],
        routes=[Route("192.0.2.0/24", None, "eth0", 0)] + list(routes or []),
        resolvers=list(resolvers),
        source=source,
    )


def run(hn, only, **config):
    return Engine(config={"_netinfo": hn, **config}).run(only=only)


def by_name(diag):
    return diag.results_by_name()


def pings(answered=(), lost=(), error=None):
    def fake(target, **kw):
        if error:
            return PingStats(target=target, error=error)
        if target in answered:
            return PingStats(
                target=target, address=target, sent=4, received=4, rtts_ms=[0.5] * 4
            )
        if target in lost:
            return PingStats(
                target=target, address=target, sent=10, received=9, rtts_ms=[0.5] * 9
            )
        return PingStats(target=target, address=target, sent=4, received=0)

    return fake


@pytest.fixture
def world(monkeypatch):
    """Fake pings, ARP and TCP; tests say what answers."""

    class World:
        answered = set()
        arp_before = None
        arp_after = None
        table = []
        tcp_ok = False
        tcp_tries = []

    w = World()
    state = {"pinged": False}

    def ping(target, **kw):
        state["pinged"] = True
        return pings(answered=w.answered)(target)

    def arp(ip):
        return w.arp_after if state["pinged"] else w.arp_before

    def tcp(host, port, timeout=3.0):
        w.tcp_tries.append((host, port))
        return w.tcp_ok, 1.0, "connected" if w.tcp_ok else "timeout"

    monkeypatch.setattr(gateway, "ping", ping)
    monkeypatch.setattr(icmp, "_ping", ping)
    monkeypatch.setattr(netinfo, "neighbor", arp)
    monkeypatch.setattr(gateway, "tcp_connect_once", tcp)
    monkeypatch.setattr(
        neighbor, "_neighbours", lambda: w.table if state["pinged"] else []
    )
    return w


# -- interface: engine-1, engine-2 -------------------------------------------


def test_virtual_adapters_do_not_make_an_unplugged_pc_healthy():
    hn = HostNetwork(
        interfaces=[
            nic("Ethernet", "192.0.2.10", up=False),
            nic("Wi-Fi", up=False, kind=KIND_WIFI),
            nic("VMware Network Adapter VMnet1", "198.51.100.1", kind=KIND_VIRTUAL),
        ]
    )
    diag = run(hn, ["interface"])
    res = by_name(diag)["interface"]
    assert (res.status, res.code) == (Status.FAIL, "interface.no_link")
    assert "VMware Network Adapter VMnet1 is virtual and does not count" in res.summary
    assert diag.status == Status.FAIL


def test_a_virtual_switch_on_a_linked_nic_still_counts():
    hn = HostNetwork(
        interfaces=[
            nic("Ethernet"),
            nic("vEthernet (External)", "192.0.2.20", kind=KIND_VIRTUAL),
        ]
    )
    res = by_name(run(hn, ["interface"]))["interface"]
    assert res.code == "interface.ok"
    assert res.summary.startswith("vEthernet (External) 192.0.2.20/24")


def test_a_container_with_only_virtual_adapters_is_fine():
    hn = HostNetwork(interfaces=[nic("eth0", "192.0.2.5", kind=KIND_VIRTUAL)])
    assert by_name(run(hn, ["interface"]))["interface"].code == "interface.ok"


def test_a_line_nic_stuck_on_apipa_is_named_while_wifi_works():
    hn = HostNetwork(
        interfaces=[
            nic("Wi-Fi", "198.51.100.23", kind=KIND_WIFI),
            nic("イーサネット", "169.254.33.7", prefix=16),
        ]
    )
    diag = run(hn, ["interface"])
    res = by_name(diag)["interface"]
    assert (res.status, res.code) == (Status.WARN, "interface.apipa")
    assert "but イーサネット only has a 169.254.x.x (APIPA) address" in res.summary
    assert diag.status == Status.WARN
    assert any("static IP" in step for step in diag.primary.runbook)


def test_a_linked_nic_without_any_ipv4_stays_quiet():
    # Bridge ports, EtherCAT NICs and a Mac's internal en5 look like this.
    hn = HostNetwork(
        interfaces=[nic("Wi-Fi", "198.51.100.23", kind=KIND_WIFI), nic("eth1")]
    )
    assert by_name(run(hn, ["interface"]))["interface"].code == "interface.ok"


# -- link: engine-15 -----------------------------------------------------------


def test_unknown_link_speed_is_left_out():
    hn = HostNetwork(interfaces=[nic("Ethernet", "192.0.2.10", speed=None)])
    res = by_name(run(hn, ["link"]))["link"]
    assert res.summary == "Ethernet full, no errors"


# -- wifi: engine-5 ------------------------------------------------------------


def _idle_iw(monkeypatch):
    """A Linux PC whose Wi-Fi adapter is not associated."""

    def iw(args, timeout=10):
        out = (
            "phy#0\n\tInterface wlp2s0\n"
            if args == ["iw", "dev"]
            else "Not connected.\n"
        )
        return CommandResult(True, 0, out, "", True)

    monkeypatch.setattr(wifi, "is_linux", lambda: True)
    monkeypatch.setattr(wifi, "have", lambda cmd: cmd == "iw")
    monkeypatch.setattr(wifi, "run", iw)


def test_an_unused_wifi_adapter_does_not_warn_on_linux(monkeypatch):
    _idle_iw(monkeypatch)
    res = by_name(run(line_pc(), ["wifi"]))["wifi"]
    assert (res.status, res.code) == (Status.SKIP, "wifi.not_associated")
    assert res.summary == "wlp2s0: not associated to any AP (Wi-Fi not in use)"


# -- clock: engine-12 ----------------------------------------------------------


@pytest.mark.parametrize(
    "internet, status", [(False, Status.SKIP), (True, Status.WARN)]
)
def test_an_unsynchronised_clock_is_normal_only_on_a_closed_network(
    monkeypatch, internet, status
):
    monkeypatch.setattr(system, "have", lambda cmd: cmd == "timedatectl")
    monkeypatch.setattr(
        system, "run", lambda args, **kw: CommandResult(True, 0, "no\n", "", True)
    )
    res = by_name(run(line_pc(), ["clock"], internet=internet))["clock"]
    assert res.status == status


# -- dualstack: engine-14 ------------------------------------------------------


def test_dualstack_sends_two_pings_per_family_whatever_the_count(monkeypatch):
    sent = []

    def fake(host, count=10, family=None, **kw):
        sent.append((family, count))
        return PingStats(target=host, sent=count, received=count, raw="reply")

    monkeypatch.setattr(dualstack, "system_ping", fake)
    monkeypatch.setattr(dualstack, "_has_family", lambda host, fam: True)
    res = dualstack.DualStackProbe().check(Context({"dns_name": "h", "count": 300}))
    assert res.code == "dualstack.ok"
    assert sent == [(4, 2), (6, 2)]


def test_a_pc_without_an_ipv6_route_is_single_stack_not_broken(monkeypatch):
    def fake(host, count=2, family=None, **kw):
        if family == 4:
            return PingStats(target=host, sent=count, received=count, raw="reply")
        return PingStats(
            target=host,
            sent=count,
            errors={"connect: Network is unreachable": count},
            raw="ping: connect: Network is unreachable\n",
        )

    monkeypatch.setattr(dualstack, "system_ping", fake)
    monkeypatch.setattr(dualstack, "_has_family", lambda host, fam: True)
    monkeypatch.setattr(dualstack, "_tcp_family", lambda host, family, **kw: None)
    res = dualstack.DualStackProbe().check(Context({"dns_name": "example.com"}))
    assert (res.status, res.code) == (Status.OK, "dualstack.single_stack")


# -- firewall: engine-11 -------------------------------------------------------

UFW = """-P INPUT DROP
-P FORWARD DROP
-P OUTPUT ACCEPT
-N ufw-before-input
-N ufw-before-output
-N ufw-user-output
-N ufw-user-limit
-N DOCKER-ISOLATION-STAGE-2
-A INPUT -j ufw-before-input
-A FORWARD -j DOCKER-ISOLATION-STAGE-2
-A OUTPUT -j ufw-before-output
-A ufw-before-input -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
-A ufw-before-input -m conntrack --ctstate INVALID -j DROP
-A ufw-before-output -o lo -j ACCEPT
-A ufw-before-output -m conntrack --ctstate INVALID -j DROP
-A ufw-before-output -j ufw-user-output
-A ufw-user-limit -j REJECT --reject-with icmp-port-unreachable
-A DOCKER-ISOLATION-STAGE-2 -o docker0 -j DROP
"""
FIREWALLD = """table inet firewalld {
\tchain filter_INPUT {
\t\ttype filter hook input priority filter + 10; policy accept;
\t\tct state { established, related } accept
\t\tct state invalid drop
\t\tjump filter_INPUT_ZONES
\t\treject with icmpx admin-prohibited
\t}
\tchain filter_INPUT_ZONES {
\t\tiifname "eth0" goto filter_IN_public
\t}
\tchain filter_IN_public {
\t\tlog prefix "drop: " reject
\t}
\tchain filter_FORWARD {
\t\ttype filter hook forward priority filter + 10; policy accept;
\t\tct state { established, related } accept
\t\tct state invalid drop
\t\tip6 daddr { ::/96, ::ffff:0.0.0.0/96, 2002::/24, 2002:a00::/24, 2002:7f00::/24, 2002:a9fe::/32, 2002:ac10::/28, 2002:c0a8::/32, 2002:e000::/19 } reject with icmpv6 addr-unreachable
\t\treject with icmpx admin-prohibited
\t}
\tchain filter_OUTPUT {
\t\ttype filter hook output priority filter + 10; policy accept;
\t\tct state { established, related } accept
\t\toifname "lo" accept
\t\tip6 daddr { ::/96, ::ffff:0.0.0.0/96, 2002::/24, 2002:a00::/24, 2002:7f00::/24, 2002:a9fe::/32, 2002:ac10::/28, 2002:c0a8::/32, 2002:e000::/19 } reject with icmpv6 addr-unreachable
\t\tjump filter_OUTPUT_POLICIES
\t}
\tchain filter_OUTPUT_POLICIES {
\t\tct state invalid drop
\t}
}
"""
NFT_OUTPUT_REJECT = """table inet filter {
\tchain output {
\t\ttype filter hook output priority filter; policy accept;
\t\tjump plc_block
\t}
\tchain plc_block {
\t\tip daddr { 192.0.2.39,
\t\t\t   192.0.2.40 } tcp dport 502 reject
\t}
}
"""
NFT_NO_REPLIES = """table inet filter {
\tchain input {
\t\ttype filter hook input priority filter; policy drop;
\t\tiifname "lo" accept
\t}
}
"""


def _firewall(monkeypatch, tool, text):
    monkeypatch.setattr(firewall, "is_linux", lambda: True)
    monkeypatch.setattr(firewall, "is_root", lambda: True)
    monkeypatch.setattr(firewall, "have", lambda cmd: cmd == tool)
    monkeypatch.setattr(
        firewall, "run", lambda args, **kw: CommandResult(True, 0, text, "", True)
    )
    return by_name(run(line_pc(), ["firewall"]))["firewall"]


@pytest.mark.parametrize("tool, text", [("iptables", UFW), ("nft", FIREWALLD)])
def test_a_stock_stateful_firewall_is_fine(monkeypatch, tool, text):
    res = _firewall(monkeypatch, tool, text)
    assert (res.status, res.code) == (Status.OK, "firewall.ok")


@pytest.mark.parametrize(
    "tool, text, summary",
    [
        (
            "iptables",
            UFW + "-A ufw-user-output -d 192.0.2.39/32 -p tcp --dport 502 -j REJECT\n",
            "iptables: 1 rule(s) drop or reject outbound traffic",
        ),
        (
            "iptables",
            UFW.replace("-P OUTPUT ACCEPT", "-P OUTPUT DROP"),
            "iptables: blocks outbound traffic by default",
        ),
        (
            "nft",
            NFT_OUTPUT_REJECT,
            "nftables: 1 rule(s) drop or reject outbound traffic",
        ),
    ],
)
def test_outbound_blocking_is_flagged(monkeypatch, tool, text, summary):
    res = _firewall(monkeypatch, tool, text)
    assert (res.status, res.code) == (Status.WARN, "firewall.outbound_block")
    assert res.summary.startswith(summary)


IPT_DOCKER_NO_REPLIES = """-P INPUT DROP
-P FORWARD DROP
-P OUTPUT ACCEPT
-N DOCKER
-A INPUT -i lo -j ACCEPT
-A INPUT -p tcp -m tcp --dport 22 -j ACCEPT
-A FORWARD -o docker0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
-A FORWARD -o docker0 -j DOCKER
"""


@pytest.mark.parametrize(
    "tool, text", [("nft", NFT_NO_REPLIES), ("iptables", IPT_DOCKER_NO_REPLIES)]
)
def test_an_input_drop_without_established_eats_the_replies(monkeypatch, tool, text):
    res = _firewall(monkeypatch, tool, text)
    assert (res.status, res.code) == (Status.WARN, "firewall.replies_blocked")


def nft_output(rule):
    return (
        "table inet filter {\n"
        "\tchain input {\n"
        "\t\ttype filter hook input priority filter; policy drop;\n"
        "\t\tct state established,related accept\n"
        "\t}\n"
        "\tchain output {\n"
        "\t\ttype filter hook output priority filter; policy accept;\n"
        f"\t\t{rule}\n"
        "\t}\n"
        "}\n"
    )


@pytest.mark.parametrize(
    "rule",
    [
        "ip6 daddr ::ffff:0.0.0.0/96 drop",
        "ip6 daddr ::ffff:192.0.2.1 reject",
        "ip6 daddr { 2002:c0a8::/32,\n\t\t\t    2002:a00::/24 } meta l4proto tcp reject",
        "ip6 daddr { 2002:e000::/20, ::/96 } counter reject with icmpv6 no-route",
    ],
)
def test_rules_aimed_only_at_addresses_this_pc_never_sends_to_are_fine(
    monkeypatch, rule
):
    res = _firewall(monkeypatch, "nft", nft_output(rule))
    assert (res.status, res.code) == (Status.OK, "firewall.ok")


@pytest.mark.parametrize(
    "rule",
    [
        "ip6 daddr 2001:db8::50 tcp dport 502 reject",
        "ip6 daddr { ::ffff:0.0.0.0/96, 2001:db8::/32 } reject",
        "ip6 daddr != { ::/96, ::ffff:0.0.0.0/96 } reject",
        "ip6 daddr 2002::/16 drop",
        "ip6 daddr @blocked drop",
    ],
)
def test_ipv6_rules_that_reach_real_destinations_still_warn(monkeypatch, rule):
    res = _firewall(monkeypatch, "nft", nft_output(rule))
    assert (res.status, res.code) == (Status.WARN, "firewall.outbound_block")
    assert res.summary.startswith("nftables: 1 rule(s) drop or reject outbound")


# -- gateway: engine-3, engine-7, engine-10, routes, link defaults -------------

INTERNET = [Route("0.0.0.0/0", GW, "eth0", 100)]


def test_a_gateway_that_answers_arp_but_not_ping_does_not_hide_the_internet(world):
    world.answered = {"1.1.1.1"}
    world.arp_after = Neighbor(GW, GW_MAC, "reachable", "eth0", confirmed_age=0.2)
    diag = run(line_pc(INTERNET), ["interface", "gateway", "external"], internet=True)
    res = by_name(diag)
    assert (res["gateway"].status, res["gateway"].code) == (
        Status.WARN,
        "gateway.icmp_blocked",
    )
    assert "(it answers ARP)" in res["gateway"].summary
    assert res["external"].status == Status.OK
    assert diag.status == Status.WARN
    assert world.tcp_tries == []


def test_traffic_through_a_silent_gateway_proves_it_is_there(world):
    world.answered = {"1.1.1.1"}
    world.arp_before = world.arp_after = Neighbor(GW, GW_MAC, "stale", "eth0")
    world.tcp_ok = True
    res = by_name(
        run(line_pc(INTERNET), ["interface", "gateway", "external"], internet=True)
    )
    assert res["gateway"].code == "gateway.icmp_blocked"
    assert "traffic through it (1.1.1.1:443) works" in res["gateway"].summary
    assert res["external"].status == Status.OK


def test_a_gateway_that_answers_nothing_still_fails(world):
    world.arp_after = Neighbor(GW, None, "failed", "eth0")
    diag = run(line_pc(INTERNET), ["interface", "gateway", "external"], internet=True)
    res = by_name(diag)
    assert (res["gateway"].status, res["gateway"].code) == (
        Status.FAIL,
        "gateway.unreachable",
    )
    assert "no ARP reply either" in res["gateway"].summary
    assert res["external"].code == "external.not_evaluated"
    assert world.tcp_tries == []


def test_a_silent_gateway_on_a_closed_line_is_a_warning(world):
    world.arp_after = Neighbor(GW, None, "failed", "eth0")
    world.table = [world.arp_after]
    diag = run(line_pc(INTERNET), ["interface", "gateway", "neighbor"], internet=False)
    res = by_name(diag)
    assert (res["gateway"].status, res["gateway"].code) == (
        Status.WARN,
        "gateway.unreachable_closed",
    )
    assert res["neighbor"].status == Status.OK
    assert diag.status == Status.WARN
    assert any("clear the gateway field of eth0" in s for s in diag.primary.runbook)


def gw_arp(state, confirmed_age=None):
    return Neighbor(GW, GW_MAC, state, "eth0", confirmed_age=confirmed_age)


@pytest.mark.parametrize(
    "before, after, status, code",
    [
        (
            gw_arp("reachable", 25.0),
            gw_arp("reachable", 26.0),
            Status.FAIL,
            "gateway.unreachable",
        ),
        (gw_arp("reachable"), gw_arp("reachable"), Status.FAIL, "gateway.unreachable"),
        (
            gw_arp("reachable", 25.0),
            gw_arp("reachable", 0.5),
            Status.WARN,
            "gateway.icmp_blocked",
        ),
        (gw_arp("stale"), gw_arp("reachable"), Status.WARN, "gateway.icmp_blocked"),
    ],
    ids=[
        "linux-confirmed-25s-before-the-test",
        "no-age-reachable-before-and-after",
        "linux-confirmed-during-the-test",
        "no-age-reachable-only-after",
    ],
)
def test_only_arp_confirmed_during_the_test_proves_a_silent_gateway(
    world, before, after, status, code
):
    world.arp_before, world.arp_after = before, after
    diag = run(line_pc(INTERNET), ["interface", "gateway", "external"], internet=True)
    res = by_name(diag)["gateway"]
    assert (res.status, res.code) == (status, code)
    assert ("is present" in res.summary) == (status == Status.WARN)
    blamed = "gateway" if status == Status.FAIL else "external"
    assert diag.primary.cause == blamed, diag.primary


def test_the_advice_points_at_the_equipment_nic_gateway(world):
    hn = HostNetwork(
        interfaces=[
            nic("Wi-Fi", "198.51.100.23", kind=KIND_WIFI),
            nic("イーサネット", "192.0.2.10"),
        ],
        routes=[
            Route("0.0.0.0/0", "198.51.100.1", "Wi-Fi", 50),
            Route("0.0.0.0/0", GW, "イーサネット", 25),
        ],
    )
    world.arp_after = Neighbor(GW, None, "failed", "イーサネット")
    diag = run(hn, ["gateway"], internet=True)
    assert diag.primary.code == "gateway.unreachable"
    assert "198.51.100.1 (Wi-Fi)" in diag.primary.summary
    assert diag.primary.runbook[0].startswith(
        "Remove the default gateway from イーサネット's IPv4 settings"
    )


def test_gateway_advice_follows_the_result(monkeypatch):
    monkeypatch.setattr(gateway, "ping", pings(lost={GW}))
    monkeypatch.setattr(netinfo, "neighbor", lambda ip: None)
    diag = run(line_pc(INTERNET), ["gateway"])
    assert diag.primary.code == "gateway.degraded"
    assert "answers, but with loss or delay" in diag.primary.summary
    assert "not responding" not in diag.primary.summary


def test_a_gateway_that_cannot_be_pinged_is_not_measured(monkeypatch):
    monkeypatch.setattr(gateway, "ping", pings(error="ping command not available"))
    monkeypatch.setattr(netinfo, "neighbor", lambda ip: None)
    diag = run(line_pc(INTERNET), ["gateway"])
    assert by_name(diag)["gateway"].status == Status.UNKNOWN
    assert diag.status == Status.UNKNOWN
    assert diag.status.exit_code == ExitCode.INCOMPLETE


def test_unreadable_routes_are_not_measured_rather_than_no_gateway():
    hn = HostNetwork(
        interfaces=[nic("eth0", "192.0.2.10")],
        errors=["could not read the routing table ('ip' failed)"],
    )
    res = by_name(run(hn, ["gateway"], internet=True))["gateway"]
    assert (res.status, res.code) == (Status.UNKNOWN, "gateway.routes_unknown")
    assert res.summary == "could not read the routes"


def test_a_point_to_point_default_has_no_router_to_ping(world):
    hn = line_pc([Route("0.0.0.0/0", None, "ppp0", 0)])
    res = by_name(run(hn, ["gateway"], internet=True))["gateway"]
    assert (res.status, res.code) == (Status.SKIP, "gateway.via_link")
    assert res.summary == "traffic leaves via ppp0, no router to ping"


# -- neighbor: engine-6, known #4 ----------------------------------------------


@pytest.mark.parametrize("jobs", [1, 4])
def test_the_arp_table_is_read_after_the_gateway_was_pinged(world, jobs):
    world.answered = {GW}
    world.table = [Neighbor(GW, GW_MAC, "reachable", "eth0")]
    diag = Engine(config={"_netinfo": line_pc(INTERNET)}).run(
        only=["interface", "gateway", "neighbor"], jobs=jobs
    )
    res = by_name(diag)
    assert (
        res["neighbor"].summary
        == f"1 neighbours, no conflicts; gateway {GW} at {GW_MAC}"
    )
    assert diag.status == Status.OK


def test_one_ip_on_two_nics_is_not_a_conflict(world):
    world.answered = {GW}
    world.table = [
        Neighbor(GW, GW_MAC, "reachable", "eth0"),
        Neighbor(GW, "00:00:5e:00:53:02", "reachable", "wlan0"),
    ]
    res = by_name(run(line_pc(INTERNET), ["gateway", "neighbor"]))["neighbor"]
    assert res.code == "neighbor.ok"


def test_two_macs_for_one_ip_on_one_link_are_a_conflict(world):
    world.answered = {GW}
    world.table = [
        Neighbor("192.0.2.39", "00:00:5e:00:53:39", "reachable", "eth0"),
        Neighbor("192.0.2.39", "00:00:5e:00:53:40", "stale", "eth0"),
    ]
    res = by_name(run(line_pc(INTERNET), ["gateway", "neighbor"]))["neighbor"]
    assert (res.status, res.code) == (Status.FAIL, "neighbor.duplicate_ip")


# -- dns: engine-8, measure-3, engine-4, engine-10 -----------------------------


def _answer(server, rcode="NOERROR", addresses=("192.0.2.80",), error=None):
    return DnsAnswer(
        server=server,
        name="example.com",
        addresses=list(addresses) if rcode == "NOERROR" else [],
        rcode=None if error else rcode,
        elapsed_ms=None if error else 5.0,
        error=error,
    )


@pytest.fixture
def resolvers(monkeypatch):
    def install(answers, cached=("192.0.2.80",)):
        monkeypatch.setattr(dns, "configured_resolvers", lambda: list(answers))
        monkeypatch.setattr(
            dns, "_resolve", lambda name, timeout=3.0: list(cached) if cached else None
        )

        def query(name, server, timeout=2.0):
            a = answers[server]
            if isinstance(a, Exception):
                raise a
            return a

        monkeypatch.setattr(dns, "query", query)

    return install


def test_every_server_dead_is_a_failure_even_with_a_cached_answer(resolvers):
    resolvers({"192.0.2.53": _answer("192.0.2.53", error="timeout")})
    res = by_name(run(line_pc(), ["dns"]))["dns"]
    assert (res.status, res.code) == (Status.FAIL, "dns.all_servers_down")
    assert "resolves only from the cache or hosts file" in res.summary


def test_a_refusing_server_is_a_bad_server(resolvers):
    resolvers(
        {
            "192.0.2.53": _answer("192.0.2.53", rcode="REFUSED"),
            "192.0.2.54": _answer("192.0.2.54"),
        }
    )
    res = by_name(run(line_pc(), ["dns"]))["dns"]
    assert (res.status, res.code) == (Status.WARN, "dns.server_down")
    assert "192.0.2.53 REFUSED" in res.summary


def test_no_such_name_is_an_answer(resolvers):
    resolvers({"192.0.2.53": _answer("192.0.2.53", rcode="NXDOMAIN")})
    assert by_name(run(line_pc(), ["dns"]))["dns"].code == "dns.ok"


def test_a_garbled_reply_marks_only_that_server(resolvers):
    resolvers(
        {
            "192.0.2.53": DnsReplyError("reply cut short"),
            "192.0.2.54": _answer("192.0.2.54"),
        }
    )
    res = by_name(run(line_pc(), ["dns"]))["dns"]
    assert (res.status, res.code) == (Status.WARN, "dns.server_down")
    assert "192.0.2.53 no answer (bad reply: reply cut short)" in res.summary


def test_a_garbled_reply_from_a_scenario_resolver_fails_that_resolver(monkeypatch):
    def query(name, server, timeout=2.0):
        raise DnsReplyError("reply cut short")

    monkeypatch.setattr(dns, "query", query)
    res = dns.DnsProbe().check(Context({"_resolver": "192.0.2.53"}))
    assert (res.status, res.code) == (Status.FAIL, "dns.failed")
    assert "bad reply: reply cut short" in res.summary


@pytest.mark.parametrize(
    "server, dns_code",
    [("192.0.2.53", "dns.ok"), ("203.0.113.53", "dns.not_evaluated")],
)
def test_dns_waits_on_the_internet_only_for_resolvers_off_the_lan(
    resolvers, world, server, dns_code
):
    resolvers({server: _answer(server)})
    diag = run(line_pc(), ["interface", "gateway", "external", "dns"], internet=True)
    res = by_name(diag)
    assert res["gateway"].code == "gateway.none"
    assert res["dns"].code == dns_code


def test_dns_advice_claims_connectivity_only_when_it_was_measured(resolvers):
    resolvers({"192.0.2.53": _answer("192.0.2.53", error="timeout")}, cached=None)
    diag = run(line_pc(), ["dns"], dns_name="plc1.line.example")
    assert "Connectivity is fine" not in diag.primary.summary
    assert diag.primary.runbook[0] == (
        "Test the resolver directly: nslookup plc1.line.example 192.0.2.53"
    )


# -- ipconfig: one multipath default -------------------------------------------


def _two_nics(routes, source):
    return HostNetwork(
        interfaces=[nic("eth0", "192.0.2.10"), nic("eth1", "198.51.100.10")],
        routes=routes,
        source=source,
    )


def test_one_multipath_default_is_not_two_rival_gateways():
    ecmp = [
        Route("0.0.0.0/0", GW, "eth0", 100),
        Route("0.0.0.0/0", "198.51.100.1", "eth1", 100),
    ]
    res = by_name(run(_two_nics(ecmp, "linux:ip-json"), ["ipconfig"]))["ipconfig"]
    assert res.code == "ipconfig.ok"


@pytest.mark.parametrize(
    "metrics, source",
    [((100, 600), "linux:ip-json"), ((25, 25), "macos")],
)
def test_defaults_on_two_nics_still_warn(metrics, source):
    routes = [
        Route("0.0.0.0/0", GW, "eth0", metrics[0]),
        Route("0.0.0.0/0", "198.51.100.1", "eth1", metrics[1]),
    ]
    res = by_name(run(_two_nics(routes, source), ["ipconfig"]))["ipconfig"]
    assert res.code == "ipconfig.multi_gateway"


# -- a healthy, ordinary laptop still exits 0 ----------------------------------


def test_a_healthy_laptop_with_idle_wifi_and_vm_adapters_is_healthy(world, monkeypatch):
    world.answered = {GW}
    world.table = [Neighbor(GW, GW_MAC, "reachable", "eth0")]
    _idle_iw(monkeypatch)
    hn = HostNetwork(
        interfaces=[
            nic("eth0", "192.0.2.10"),
            nic("wlp2s0", up=False, kind=KIND_WIFI),
            nic("vmnet8", "198.51.100.1", kind=KIND_VIRTUAL),
        ],
        routes=[Route("0.0.0.0/0", GW, "eth0", 100)],
        source="linux:ip-json",
    )
    diag = run(
        hn,
        ["interface", "ipconfig", "link", "wifi", "gateway", "neighbor"],
        internet=False,
    )
    assert diag.status == Status.OK, [(r.name, r.summary) for r in diag.results]
