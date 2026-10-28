"""``netdiag collect``: raw output capture and structure-preserving masking."""

import json
import zipfile

from click.testing import CliRunner

from netdiag import collect as collect_mod
from netdiag import netinfo
from netdiag.cli import cli
from netdiag.collect import Captured, Masker, plan, run_collect
from netdiag.netinfo.model import KIND_ETHERNET, Address, HostNetwork, Interface, Route

WIN_ARP_JA = (
    "インターフェイス: 192.168.1.14 --- 0xd\r\n"
    "  インターネット アドレス 物理アドレス           種類\r\n"
    "  192.168.1.1           cc-1a-fa-c0-a0-6e     動的\r\n"
    "  255.255.255.255       ff-ff-ff-ff-ff-ff     静的\r\n"
).encode("cp932")


def test_ipv4_mapping_keeps_subnets_last_octet_and_specials():
    m = Masker()
    out = m.mask(
        b"192.168.3.39 192.168.3.1 192.168.4.1 255.255.255.0 0.0.0.0 127.0.0.1 169.254.1.2 1.1.1.1"
    )
    assert (
        out
        == b"10.100.0.39 10.100.0.1 10.100.1.1 255.255.255.0 0.0.0.0 127.0.0.1 169.254.1.2 1.1.1.1"
    )


def test_mac_keeps_vendor_prefix_and_is_consistent():
    m = Masker()
    a = m.mask(b"00-26-92-AA-BB-CC and 00:26:92:aa:bb:cc and ff-ff-ff-ff-ff-ff")
    assert a.startswith(b"00-26-92-") and b"AA-BB-CC" not in a
    first, second = a.split(b" and ")[:2]
    assert first.replace(b"-", b":").lower() == second.lower()  # same device, same mask
    assert a.endswith(b"ff-ff-ff-ff-ff-ff")


def test_shift_jis_text_survives_masking():
    m = Masker()
    out = m.mask(WIN_ARP_JA)
    text = out.decode("cp932")
    assert "インターフェイス" in text and "動的" in text and "静的" in text
    assert "192.168" not in text and "cc-1a-fa-" in text and "c0-a0-6e" not in text


def test_ipv6_ssid_host_user_and_domains():
    m = Masker(hostnames=["LINE1-ENG01"], users=["tanaka"])
    out = m.mask(
        b"fe80::20c:29ff:fe02:c855%12 at 14:03:12\n"
        b"    SSID                   : Factory-3F\n"
        b"Host Name: line1-eng01 C:\\Users\\tanaka\n"
        b"Suffix: corp.example-mfg.co.jp example.com"
    ).decode()
    assert "fe80::1%12" in out and "14:03:12" in out  # times are not IPv6
    assert "Factory-3F" not in out and "SSID-1" in out
    assert "PC-HOST" in out and "tanaka" not in out
    assert "example-mfg" not in out and "example.com" in out


def test_plan_covers_each_os():
    win = {c.name for c in plan("windows", "192.168.1.1", ["192.168.3.39"])}
    assert {
        "ps_collect.json",
        "arp_a.txt",
        "route_print.txt",
        "ping_gateway.txt",
        "ping_target_0.txt",
    } <= win
    lin = {c.name for c in plan("linux", None, [], ping=False)}
    assert "ip_addr.json" in lin and "ping_group_range.txt" in lin
    assert not any(
        n.startswith(("ping_gateway", "ping_testnet", "tracepath")) for n in lin
    )
    mac = {c.name for c in plan("macos", "10.0.0.1", [])}
    assert {"ifconfig.txt", "netstat_ibn.txt", "ping_unknown_host.txt"} <= mac


def _fake_env(monkeypatch):
    hn = HostNetwork(
        interfaces=[
            Interface(
                name="eth0",
                kind=KIND_ETHERNET,
                up=True,
                mac="00:26:92:aa:bb:cc",
                addresses=[Address("192.168.3.100", 24)],
            )
        ],
        routes=[Route("0.0.0.0/0", gateway="192.168.3.1", interface="eth0")],
    )
    monkeypatch.setattr(netinfo, "collect", lambda: hn)
    monkeypatch.setattr(netinfo, "snapshot", lambda max_age=5.0: hn)
    monkeypatch.setattr(collect_mod, "os_name", lambda: "windows")

    import netdiag.measure as measure
    import netdiag.runner as runner
    from netdiag.measure import PingStats

    monkeypatch.setattr(
        measure,
        "ping_many",
        lambda targets, **kw: {
            t: PingStats(target=t, sent=4, received=4, rtts_ms=[0.5] * 4)
            for t in targets
        },
    )
    monkeypatch.setattr(
        runner, "run_check", lambda devices, config=None, **kw: _StubReport()
    )


class _StubReport:
    def to_dict(self):
        return {
            "status": "ok",
            "host": {"results": [{"summary": "eth0 192.168.3.100/24"}]},
        }


def test_run_collect_writes_raw_bytes_and_interpretation(monkeypatch, tmp_path):
    _fake_env(monkeypatch)
    seen = []

    def runner(cmd):
        seen.append(cmd.name)
        if cmd.name == "arp_a.txt":
            return Captured(cmd.name, cmd.args, 0, WIN_ARP_JA)
        if cmd.name == "netsh_wlan.txt":
            return Captured(cmd.name, cmd.args, None, found=False)
        return Captured(cmd.name, cmd.args, 0, b"ok 192.168.3.39\r\n", b"")

    out = tmp_path / "c.zip"
    res = run_collect(out, mask=True, targets=["192.168.3.39"], runner=runner)
    assert "netsh_wlan.txt" in res.missing
    z = zipfile.ZipFile(out)
    names = z.namelist()
    assert (
        "netdiag-collect/README.txt" in names
        and "netdiag-collect/netdiag/check.json" in names
    )
    arp = z.read("netdiag-collect/commands/arp_a.txt.out")
    assert arp.decode("cp932").startswith(
        "インターフェイス: 10."
    )  # raw cp932 bytes, masked
    manifest = json.loads(z.read("netdiag-collect/manifest.json"))
    assert manifest["masked"] is True and manifest["os"] == "windows"
    ps = next(c for c in manifest["commands"] if c["name"] == "ps_collect.json")
    assert ps["args"][-1] == "<script>"  # the long script is not repeated
    ping = json.loads(z.read("netdiag-collect/netdiag/ping.json"))
    assert all(not k.startswith("192.168.") for k in ping)
    assert "ping_target_0.txt" in seen


def test_no_mask_keeps_values(monkeypatch, tmp_path):
    _fake_env(monkeypatch)
    out = tmp_path / "c.zip"
    run_collect(
        out,
        mask=False,
        runner=lambda cmd: Captured(cmd.name, cmd.args, 0, b"192.168.3.39"),
    )
    z = zipfile.ZipFile(out)
    assert z.read("netdiag-collect/commands/arp_a.txt.out") == b"192.168.3.39"


def test_collect_command(monkeypatch, tmp_path):
    _fake_env(monkeypatch)
    monkeypatch.setattr(
        collect_mod, "run_raw", lambda cmd: Captured(cmd.name, cmd.args, 0, b"x")
    )
    out = tmp_path / "field.zip"
    res = CliRunner().invoke(cli, ["collect", "-o", str(out), "--no-ping"])
    assert res.exit_code == 0, res.output
    assert out.exists() and "masked" in res.output
