"""``netdiag collect`` masks every MAC, SSID and internal domain it promises to.

The Windows samples are reconstructed by hand (Japanese Windows 11, cp932),
not captured from a real PC.
"""

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

ROUTE_PRINT_JA = (
    "===========================================================================\r\n"
    "インターフェイス一覧\r\n"
    " 12...00 15 5d 01 02 03 ......Hyper-V Virtual Ethernet Adapter\r\n"
    "  5...3c a9 f4 12 34 56 ......Intel(R) Wi-Fi 6 AX201 160MHz\r\n"
    "  1...........................Software Loopback Interface 1\r\n"
    "===========================================================================\r\n"
).encode("cp932")

IPCONFIG_JA = (
    "\r\nWindows IP 構成\r\n\r\n"
    "   ホスト名. . . . . . . . . . . . . . . .: LINE1-PC\r\n"
    "   プライマリ DNS サフィックス . . . . . . .: factory.local\r\n"
    "   DNS サフィックス検索一覧. . . . . . . .: factory.local\r\n"
    "                                            contoso.co.jp\r\n\r\n"
    "イーサネット アダプター イーサネット:\r\n\r\n"
    "   接続固有の DNS サフィックス . . . . .: contoso.com\r\n"
    "   物理アドレス. . . . . . . . . . . . . .: 00-15-5D-01-02-03\r\n"
    "   DHCPv6 クライアント DUID. . . . . . . .: "
    "00-01-00-01-2A-BC-DE-F0-00-15-5D-01-02-03\r\n"
    "   DNS サーバー. . . . . . . . . . . . . .: 192.0.2.1\r\n"
).encode("cp932")

NETSH_WLAN_JA = (
    "\r\n    名前                   : Wi-Fi\r\n"
    "    物理アドレス           : 3c:a9:f4:12:34:56\r\n"
    "    状態                   : 接続されました\r\n"
    f"    SSID                   : {SSID}\r\n"
    "    BSSID                  : 00:1a:2b:3c:4d:5e\r\n"
    "    接続モード             : プロファイル\r\n"
    "    シグナル               : 92%\r\n"
    f"    プロファイル           : {SSID}\r\n"
).encode("cp932")

IW_DEV = (
    f"phy#0\n\tInterface wlp2s0\n\t\taddr 3c:a9:f4:12:34:56\n\t\tssid {SSID}\n"
).encode()

RESOLV_CONF = b"# managed by man:systemd-resolved(8)\nnameserver 127.0.0.53\nsearch factory.local\n"

SCUTIL_DNS = (
    b"resolver #1\n  search domain[0] : factory.local\n  nameserver[0] : 192.0.2.1\n"
)


def _all_forms(mac):
    hexes = re.findall(r"[0-9a-f]{2}", mac.lower())
    return {
        sep.join(h.upper() if upper else h for h in hexes)
        for sep in ("-", ":", " ")
        for upper in (True, False)
    }


def test_windows_mac_forms_are_masked_consistently():
    m = Masker()
    route = m.mask(ROUTE_PRINT_JA).decode("cp932")
    ipcfg = m.mask(IPCONFIG_JA).decode("cp932")
    for form in _all_forms("00155d010203") | _all_forms("3ca9f4123456"):
        assert form not in route + ipcfg, form
    nic = re.search(r"物理アドレス[ .]*: (\S+)", ipcfg).group(1)
    assert nic.startswith("00-15-5D-")
    in_route = re.search(r"12\.\.\.(\S\S \S\S \S\S \S\S \S\S \S\S)", route).group(1)
    assert in_route.replace(" ", "-").upper() == nic
    duid = re.search(r"DUID[ .]*: (\S+)", ipcfg).group(1)
    assert duid.startswith("00-01-") and duid.endswith(nic)
    assert "2A-BC-DE-F0" not in duid


def test_ssids_in_profile_and_iw_lines_are_masked():
    m = Masker()
    netsh = m.mask(NETSH_WLAN_JA).decode("cp932")
    iw = m.mask(IW_DEV).decode()
    assert SSID not in netsh + iw
    assert "接続モード             : プロファイル" in netsh
    assert "BSSID" in netsh


def test_two_label_internal_domains_are_masked_and_codes_are_not():
    m = Masker()
    out = b"\n".join(m.mask(x) for x in (IPCONFIG_JA, RESOLV_CONF, SCUTIL_DNS)).decode(
        "cp932"
    )
    for dom in ("factory.local", "contoso.com", "contoso.co.jp"):
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
