"""``netdiag collect`` masks every MAC, SSID and internal domain it promises to."""

import ipaddress
import json
import re
import zipfile

import pytest

from netdiag import collect as collect_mod
from netdiag import netinfo
from netdiag.cli import main
from netdiag.collect import Captured, Masker, run_collect
from netdiag.netinfo.model import KIND_WIFI, Address, HostNetwork, Interface, Route

SSID = "FactoryWiFi-Line1"

IW_DEV = (
    f"phy#0\n\tInterface wlp2s0\n\t\taddr 3c:a9:f4:12:34:56\n\t\tssid {SSID}\n"
).encode()

RESOLV_CONF = b"# managed by man:systemd-resolved(8)\nnameserver 127.0.0.53\nsearch factory.local contoso.co.jp\n"

SCUTIL_DNS = (
    b"resolver #1\n  search domain[0] : factory.local\n  nameserver[0] : 192.0.2.1\n"
)


IW_LINK = (
    f"Connected to 00:1a:2b:3c:4d:5e (on wlp2s0)\n\tSSID: {SSID}\n\tfreq: 5180\n"
).encode()


def test_ssids_in_iw_lines_are_masked():
    m = Masker()
    iw = m.mask(IW_DEV).decode() + m.mask(IW_LINK).decode()
    assert SSID not in iw
    assert "\tSSID: SSID-1\n" in iw and "\t\tssid SSID-1\n" in iw


def test_two_label_internal_domains_are_masked_and_codes_are_not():
    m = Masker()
    out = b"\n".join(m.mask(x) for x in (RESOLV_CONF, SCUTIL_DNS)).decode()
    for dom in ("factory.local", "contoso.co.jp"):
        assert dom not in out, dom
    assert "systemd-resolved" in out
    codes = m.mask(b'{"code": "wifi.ok", "next": "gateway.ok", "web": "example.com"}')
    assert codes == b'{"code": "wifi.ok", "next": "gateway.ok", "web": "example.com"}'


IP_J_ADDR = (
    b'[{"ifindex":2,"ifname":"eth0","link_type":"ether",'
    b'"address":"3c:a9:f4:12:34:56","broadcast":"ff:ff:ff:ff:ff:ff",'
    b'"altnames":["enp2s5","enx3ca9f4123456"],"addr_info":[]}]'
)
IP_O_LINK = b"3: wlx00155d010203: <BROADCAST,MULTICAST,UP> mtu 1500\n"


def test_macs_inside_interface_names_are_masked_like_every_other_mac():
    m = Masker()
    link = json.loads(m.mask(IP_J_ADDR))[0]
    assert "3ca9f4123456" not in json.dumps(link)
    masked = link["address"].replace(":", "")
    assert masked.startswith("3ca9f4")
    assert link["altnames"] == ["enp2s5", "enx" + masked]
    named_only = m.mask(IP_O_LINK).decode()
    assert "00155d010203" not in named_only
    assert re.match(r"3: wlx00155d[0-9a-f]{6}: ", named_only)


def test_a_learned_mac_is_masked_without_separators_and_other_hex_is_not():
    m = Masker()
    m.learn(b"Physical Address: 00-15-5D-01-02-03\n")
    guid = m.mask(b"{c0a8e1f2-3b4c-11d0-8e2f-00155D010203} build deadbeef0123\n")
    assert b"00155D010203" not in guid and b"-00155D" in guid
    assert b"deadbeef0123" in guid


def test_masked_ipv4_stays_private_however_many_networks_there_are():
    m = Masker()
    nets = [(10, b) for b in range(256)] + [(172, b) for b in range(16, 32)]
    nets += [(100, b) for b in range(64, 128)]
    masked = [m.mask(f"{a}.{b}.7.9".encode()).decode() for a, b in nets]
    assert all(ipaddress.ip_address(ip).is_private for ip in masked)
    assert len({ip.rsplit(".", 2)[0] for ip in masked[:273]}) == 273


def _masked_network(m, cidr):
    ip, plen = cidr.split("/")
    return ipaddress.ip_network(f"{m.mask(ip.encode()).decode()}/{plen}")


def test_ipv4_masking_keeps_16_and_24_relationships():
    m = Masker()
    net16 = _masked_network(m, "198.18.0.0/16")
    lan = _masked_network(m, "198.51.100.0/24")
    gw = ipaddress.ip_address(m.mask(b"198.18.0.1").decode())
    same16 = ipaddress.ip_address(m.mask(b"198.18.200.9").decode())
    assert gw in net16 and same16 in net16
    assert not net16.overlaps(lan)
    assert m.mask(b"198.51.100.39").endswith(b".39")


def test_collect_bundle_has_no_ssid_anywhere(monkeypatch, tmp_path):
    hn = HostNetwork(
        interfaces=[
            Interface(
                name="wlp2s0",
                kind=KIND_WIFI,
                up=True,
                mac="3c:a9:f4:12:34:56",
                addresses=[Address("192.0.2.100", 24)],
            )
        ],
        routes=[Route("0.0.0.0/0", gateway="192.0.2.1", interface="wlp2s0")],
    )
    monkeypatch.setattr(netinfo, "collect", lambda: hn)
    monkeypatch.setattr(collect_mod, "os_name", lambda: "linux")

    class _Report:
        def to_dict(self):
            return {
                "host": {
                    "results": [
                        {
                            "name": "wifi",
                            "summary": f"wlp2s0 → {SSID}, -54 dBm",
                            "code": "wifi.ok",
                            "detail": {
                                "iface": "wlp2s0",
                                "raw": f"Connected to 00:1a:2b:3c:4d:5e\n\tSSID: {SSID}\n",
                            },
                        },
                        {
                            "name": "wifi",
                            "detail": {"iface": "Wi-Fi", "ssid": SSID},
                        },
                    ]
                }
            }

    import netdiag.runner as runner

    monkeypatch.setattr(runner, "run_check", lambda devices, config=None: _Report())

    def fake(cmd):
        out = IW_DEV if cmd.name == "iw_dev.txt" else b"ok\n"
        return Captured(cmd.name, cmd.args, 0, out)

    out = tmp_path / "c.zip"
    run_collect(out, mask=True, ping=False, runner=fake)
    with zipfile.ZipFile(out) as z:
        for name in z.namelist():
            assert SSID.encode() not in z.read(name), name
        check = json.loads(z.read("netdiag-collect/netdiag/check.json"))
    assert check["host"]["results"][0]["code"] == "wifi.ok"


def test_an_unwritable_output_is_a_usage_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(netinfo, "collect", lambda: HostNetwork())
    monkeypatch.setattr(
        collect_mod, "run_raw", lambda cmd: Captured(cmd.name, cmd.args, 0, b"x")
    )
    import netdiag.runner as runner

    monkeypatch.setattr(runner, "run_check", lambda devices, config=None: {}.get)
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    with pytest.raises(SystemExit) as exc:
        main(
            ["--lang", "en", "collect", "--no-ping", "-o", str(blocker / "c.zip")],
            prog_name="netdiag",
        )
    err = capsys.readouterr().err
    assert exc.value.code == 4
    assert "cannot write" in err and "Traceback" not in err
