"""``netdiag snapshot``: real changes on every OS, and only real changes.

Windows runs through the real PowerShell and ``arp -a`` parsers. The sample
output is reconstructed by hand (Japanese Windows 11, ``arp -a`` in cp932),
not captured.
"""

import copy
import importlib
import json

import pytest

from netdiag import netinfo
from netdiag.cli import main
from netdiag.core.util import CommandResult, decode_output
from netdiag.netinfo import windows as win
from netdiag.netinfo.model import (
    KIND_ETHERNET,
    Address,
    HostNetwork,
    Interface,
    Neighbor,
    Route,
)

cap = importlib.import_module("netdiag.snapshot.capture")

PS_COLLECT = {
    "adapters": [
        {
            "Name": "イーサネット",
            "InterfaceDescription": "Intel(R) Ethernet Connection I219-LM",
            "ifIndex": 12,
            "Status": "Up",
            "MacAddress": "3C-A9-F4-12-34-56",
            "LinkSpeed": "1 Gbps",
            "ReceiveLinkSpeed": 1000000000,
            "FullDuplex": True,
            "MtuSize": 1500,
            "Virtual": False,
            "HardwareInterface": True,
            "MediaType": "802.3",
            "PhysicalMediaType": "802.3",
        },
        {
            "Name": "Wi-Fi",
            "InterfaceDescription": "Intel(R) Wi-Fi 6 AX201 160MHz",
            "ifIndex": 5,
            "Status": "Up",
            "MacAddress": "3C-A9-F4-65-43-21",
            "LinkSpeed": "866.7 Mbps",
            "ReceiveLinkSpeed": 866700000,
            "MtuSize": 1500,
            "Virtual": False,
            "HardwareInterface": True,
            "MediaType": "Native 802.11",
            "PhysicalMediaType": "Native 802.11",
        },
    ],
    "addresses": [
        {
            "IPAddress": "192.0.2.10",
            "PrefixLength": 24,
            "InterfaceIndex": 12,
            "InterfaceAlias": "イーサネット",
            "AddressState": "Preferred",
            "PrefixOrigin": "Dhcp",
        }
    ],
    "routes": [
        {
            "DestinationPrefix": "0.0.0.0/0",
            "NextHop": "192.0.2.1",
            "InterfaceIndex": 12,
            "InterfaceAlias": "イーサネット",
            "RouteMetric": 0,
            "InterfaceMetric": 25,
        },
        {
            "DestinationPrefix": "192.0.2.0/24",
            "NextHop": "0.0.0.0",
            "InterfaceIndex": 12,
            "InterfaceAlias": "イーサネット",
            "RouteMetric": 256,
            "InterfaceMetric": 25,
        },
    ],
    "dns": [{"InterfaceIndex": 12, "ServerAddresses": ["192.0.2.1"]}],
    "stats": [
        {
            "Name": "イーサネット",
            "ReceivedUnicastPackets": 1000,
            "SentUnicastPackets": 900,
        }
    ],
    "firewall": [
        {
            "Name": "Domain",
            "Enabled": "True",
            "DefaultInboundAction": "NotConfigured",
            "DefaultOutboundAction": "NotConfigured",
        },
        {
            "Name": "Public",
            "Enabled": "True",
            "DefaultInboundAction": "Block",
            "DefaultOutboundAction": "Allow",
        },
    ],
}

ARP_A_JA = (
    "\r\nインターフェイス: 192.0.2.10 --- 0xc\r\n"
    "  インターネット アドレス 物理アドレス           種類\r\n"
    "  192.0.2.1             cc-1a-fa-c0-a0-6e     動的\r\n"
    "  192.0.2.39            00-26-92-aa-bb-cc     動的\r\n"
    "  192.0.2.255           ff-ff-ff-ff-ff-ff     静的\r\n"
)


@pytest.fixture
def windows_pc(monkeypatch, tmp_path):
    """A Japanese Windows PC; mutate ``state`` to change it between captures."""
    state = {"ps": copy.deepcopy(PS_COLLECT), "arp": ARP_A_JA}
    monkeypatch.setattr(cap, "os_name", lambda: "windows")
    monkeypatch.setattr(netinfo, "os_name", lambda: "windows")
    monkeypatch.setattr(win, "run_powershell", lambda script, timeout=30.0: state["ps"])

    def fake_run(args, timeout=10.0):
        assert args[:2] == ["arp", "-a"], args
        text = decode_output(state["arp"].encode("cp932"))
        return CommandResult(True, 0, text, "", True)

    monkeypatch.setattr(win, "run", fake_run)
    etc = tmp_path / "Windows" / "System32" / "drivers" / "etc"
    etc.mkdir(parents=True)
    (etc / "hosts").write_text("# hosts\r\n192.0.2.80 plc-gw\r\n", encoding="utf-8")
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    monkeypatch.setattr(cap, "DEFAULT_DIR", tmp_path / "snapshots")
    return state


def _run(capsys, *args, lang="en"):
    with pytest.raises(SystemExit) as exc:
        main(["--lang", lang, "snapshot", *args], prog_name="netdiag")
    out = capsys.readouterr()
    return exc.value.code, out.out, out.err


def test_windows_snapshot_captures_real_sources(windows_pc):
    sources = cap.capture()["sources"]
    assert "0.0.0.0/0 via 192.0.2.1 dev イーサネット metric 25" in sources["routes"]
    assert "イーサネット 192.0.2.10/24 dhcp" in sources["addresses"]
    assert "192.0.2.39 00:26:92:aa:bb:cc" in sources["neighbours"]
    assert sources["dns_resolvers"] == "1. 192.0.2.1"
    assert "Public: enabled=True inbound=Block outbound=Allow" in sources["firewall"]
    assert sources["hosts"] == "192.0.2.80 plc-gw"


def test_windows_default_route_change_is_reported(windows_pc, capsys):
    assert _run(capsys, "save", "ok")[0] == 0
    windows_pc["ps"]["routes"][0]["NextHop"] = "192.0.2.254"
    code, out, _ = _run(capsys, "diff", "ok")
    assert code == 2
    assert "Routing table (IPv4)" in out
    assert "+ 0.0.0.0/0 via 192.0.2.254 dev イーサネット metric 25" in out
    assert "- 0.0.0.0/0 via 192.0.2.1 dev イーサネット metric 25" in out


def test_windows_counters_wifi_rate_and_arp_churn_are_not_changes(windows_pc, capsys):
    assert _run(capsys, "save", "ok")[0] == 0
    windows_pc["ps"]["stats"][0]["ReceivedUnicastPackets"] = 99999
    windows_pc["ps"]["adapters"][1]["ReceiveLinkSpeed"] = 650000000
    windows_pc["ps"]["routes"].reverse()
    windows_pc["arp"] = (
        ARP_A_JA.replace("  192.0.2.39            00-26-92-aa-bb-cc     動的\r\n", "")
        + "  192.0.2.77            00-26-92-11-22-33     動的\r\n"
    )
    code, out, _ = _run(capsys, "diff", "ok")
    assert code == 0, out
    assert "no configuration changes" in out


def test_a_device_answering_with_another_mac_is_a_change(windows_pc, capsys):
    assert _run(capsys, "save", "ok")[0] == 0
    windows_pc["arp"] = ARP_A_JA.replace("00-26-92-aa-bb-cc", "00-0e-c6-01-02-03")
    code, out, _ = _run(capsys, "diff", "ok")
    assert code == 2
    assert "+ 192.0.2.39 00:0e:c6:01:02:03" in out


def test_japanese_headings(windows_pc, capsys):
    assert _run(capsys, "save", "ok")[0] == 0
    windows_pc["ps"]["firewall"][1]["Enabled"] = "False"
    code, out, _ = _run(capsys, "diff", "ok", lang="ja")
    assert code == 2
    assert "■ ファイアウォール" in out


def test_save_refuses_when_nothing_could_be_read(
    windows_pc, capsys, tmp_path, monkeypatch
):
    windows_pc["ps"] = None
    failed = CommandResult(False, 1, "", "", True)
    monkeypatch.setattr(win, "run", lambda args, timeout=10.0: failed)
    code, out, err = _run(capsys, "save", "empty")
    assert code == 4
    assert "nothing was saved" in err
    assert not (tmp_path / "snapshots" / "empty.json").exists()


def test_unsupported_os_refuses_instead_of_saving_an_empty_snapshot(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(cap, "os_name", lambda: "other")
    monkeypatch.setattr(cap, "DEFAULT_DIR", tmp_path)
    code, _, err = _run(capsys, "save", "x")
    assert code == 4 and "not supported" in err
    assert not (tmp_path / "x.json").exists()


def _linux_host(gateway="192.0.2.1"):
    return HostNetwork(
        source="linux:ip-json",
        interfaces=[
            Interface(
                name="eth0",
                kind=KIND_ETHERNET,
                up=True,
                mtu=1500,
                speed_mbps=1000.0,
                mac="00:26:92:aa:bb:cc",
                addresses=[
                    Address("192.0.2.10", 24, flags=["dhcp"]),
                    Address("2001:db8:1::abcd", 64, family=6),
                ],
            )
        ],
        routes=[Route("0.0.0.0/0", gateway=gateway, interface="eth0", metric=100)],
        resolvers=["192.0.2.53"],
    )


@pytest.fixture
def linux_pc(monkeypatch, tmp_path):
    state = {
        "hn": _linux_host(),
        "neigh": [Neighbor("192.0.2.1", "cc:1a:fa:c0:a0:6e", "reachable")],
        "ip -6 route show": "2001:db8:1::/64 dev eth0 proto ra metric 100 "
        "expires 1795sec pref medium\n",
        "ss -Htln": "LISTEN 0 4096 127.0.0.53%lo:53 0.0.0.0:*\n"
        "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n",
    }
    monkeypatch.setattr(cap, "os_name", lambda: "linux")
    monkeypatch.setattr(netinfo, "collect", lambda: state["hn"])
    monkeypatch.setattr(netinfo, "neighbors", lambda: state["neigh"])

    def fake_run(args, timeout=10.0):
        key = " ".join(a for a in args if not a.startswith("net.ipv4"))
        text = state.get(key, "net.ipv4.ip_forward = 0\n")
        return CommandResult(True, 0, text, "", True)

    monkeypatch.setattr(cap, "run", fake_run)
    monkeypatch.setattr(cap, "_read", lambda path: None)
    monkeypatch.setattr(cap, "DEFAULT_DIR", tmp_path)
    return state


def test_linux_lifetimes_states_and_column_widths_are_not_changes(linux_pc, capsys):
    assert _run(capsys, "save", "ok")[0] == 0
    linux_pc["hn"].interfaces[0].addresses[1] = Address(
        "2001:db8:1::9999", 64, family=6
    )
    linux_pc["neigh"] = [Neighbor("192.0.2.1", "cc:1a:fa:c0:a0:6e", "stale")]
    linux_pc["ip -6 route show"] = linux_pc["ip -6 route show"].replace("1795", "1790")
    linux_pc["ss -Htln"] = (
        "LISTEN 0   128   0.0.0.0:22                 0.0.0.0:*\n"
        "LISTEN 3   4096  127.0.0.53%lo:53           0.0.0.0:*\n"
    )
    code, out, _ = _run(capsys, "diff", "ok")
    assert code == 0, out


def test_linux_gateway_and_dns_changes_are_reported(linux_pc, capsys):
    assert _run(capsys, "save", "ok")[0] == 0
    linux_pc["hn"] = _linux_host(gateway="192.0.2.254")
    linux_pc["hn"].resolvers = ["198.51.100.53"]
    code, out, _ = _run(capsys, "diff", "ok", "--format", "json")
    assert code == 2
    changed = {c["key"] for c in json.loads(out)["changed"]}
    assert changed == {"routes", "dns_resolvers"}


def test_a_source_unreadable_at_one_end_is_not_compared(linux_pc, capsys, monkeypatch):
    real = cap.run
    monkeypatch.setattr(
        cap,
        "run",
        lambda args, timeout=10.0: (
            CommandResult(False, 127, "", "", False) if args[0] == "ss" else real(args)
        ),
    )
    assert _run(capsys, "save", "ok")[0] == 0
    monkeypatch.setattr(cap, "run", real)
    linux_pc["ss -Htln"] = "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n"
    code, out, _ = _run(capsys, "diff", "ok")
    assert code == 3
    assert "no configuration changes" in out
    assert "? Listening sockets: not compared (it could not be read when saved)" in out


@pytest.mark.parametrize("name", ["2026/11/13", "a\\b", "..", "x:y"])
def test_bad_snapshot_names_are_usage_errors(linux_pc, capsys, name):
    code, _, err = _run(capsys, "save", name)
    assert code == 4 and "cannot be a snapshot name" in err


@pytest.mark.parametrize(
    "content, problem",
    [
        ('{"version": 2, "sources": {"routes": "defa', "is damaged"),
        ('{"version": 1, "sources": {"routes": "default via x"}}', "older netdiag"),
    ],
)
def test_damaged_or_old_snapshots_are_usage_errors(
    linux_pc, capsys, tmp_path, content, problem
):
    (tmp_path / "s.json").write_text(content, encoding="utf-8")
    code, _, err = _run(capsys, "diff", "s")
    assert code == 4 and problem in err and "Traceback" not in err


def test_missing_snapshot_is_a_usage_error(linux_pc, capsys):
    code, _, err = _run(capsys, "diff", "nope")
    assert code == 4 and "no snapshot named 'nope'" in err
