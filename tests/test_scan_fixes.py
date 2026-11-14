"""`netdiag scan` sweeps every address it says it does and never loses a
result or a ledger. The vendor table names makers, not addresses."""

import pytest

import netdiag.scan as scan_mod
from netdiag import netinfo, oui
from netdiag.cli import main
from netdiag.measure import PingStats
from netdiag.netinfo.model import (
    KIND_ETHERNET,
    Address,
    HostNetwork,
    Interface,
    Neighbor,
)


def _nic(name, ip, prefix=24):
    return Interface(
        name=name, kind=KIND_ETHERNET, up=True, addresses=[Address(ip, prefix)]
    )


@pytest.fixture
def sweep(monkeypatch):
    """A fake sweep: tests set the NICs, the replies and the ARP table."""
    state = {
        "hn": HostNetwork(interfaces=[_nic("eth0", "192.0.2.100")]),
        "swept": [],
        "replies": {},
        "table": [],
        "ports": {},
    }
    monkeypatch.setattr(netinfo, "snapshot", lambda max_age=5.0: state["hn"])
    monkeypatch.setattr(netinfo, "neighbors", lambda: list(state["table"]))

    def ping_many(hosts, count=2, **kw):
        state["swept"].extend(hosts)
        out = {h: PingStats(target=h, sent=count, received=0) for h in hosts}
        out.update({h: st for h, st in state["replies"].items() if h in out})
        return out

    monkeypatch.setattr(scan_mod, "ping_many", ping_many)

    def tcp_check_many(pairs, **kw):
        from netdiag.measure.tcp import PortCheck

        return {
            (h, p): PortCheck(
                host=h, port=p, attempts=1, state=state["ports"].get(p, "closed")
            )
            for h, p in pairs
        }

    monkeypatch.setattr(scan_mod, "tcp_check_many", tcp_check_many)
    return state


def _answer(state, ip, dups=0):
    state["replies"][ip] = PingStats(
        target=ip, sent=2, received=2, duplicates=dups, rtts_ms=[0.3, 0.3]
    )


def _scan(*args):
    with pytest.raises(SystemExit) as exc:
        main(["scan", "--no-color", *args], "netdiag")
    return exc.value.code


def test_a_range_sweeps_every_address_in_it(sweep):
    assert _scan("192.0.2.10-50") in (0, None)
    assert len(sweep["swept"]) == 41
    assert {"192.0.2.12", "192.0.2.15", "192.0.2.16", "192.0.2.47"} <= set(
        sweep["swept"]
    )


def test_a_zero_padded_range_is_read_as_decimal(sweep):
    _scan("192.0.2.010-012")
    assert sweep["swept"] == ["192.0.2.10", "192.0.2.11", "192.0.2.12"]


def test_an_ipv6_range_is_refused_as_ipv6(sweep, capsys):
    assert _scan("2001:db8::/64") == 4
    assert "IPv6 ranges are not supported" in capsys.readouterr().err


def test_bare_scan_sweeps_every_line_of_a_pc_with_three_nics(sweep):
    sweep["hn"].interfaces = [
        _nic("office", "192.0.2.100"),
        _nic("line1", "198.51.100.100"),
        _nic("line2", "203.0.113.100"),
    ]
    assert _scan() in (0, None)
    assert len(sweep["swept"]) == 3 * 254


def test_save_refuses_an_existing_ledger_before_sweeping(sweep, tmp_path, capsys):
    ledger = tmp_path / "台帳.csv"
    ledger.write_text("名前,IPアドレス\nPLC1,192.0.2.39\n", encoding="utf-8")
    assert _scan("192.0.2.39", "--save", str(ledger)) == 4
    assert "already exists" in capsys.readouterr().err
    assert ledger.read_text(encoding="utf-8").startswith("名前,IPアドレス\nPLC1")
    assert sweep["swept"] == []


def test_save_into_a_missing_folder_is_refused_before_sweeping(sweep, tmp_path):
    assert _scan("192.0.2.39", "--save", str(tmp_path / "nodir" / "x.csv")) == 4
    assert sweep["swept"] == []


def test_a_failed_save_keeps_the_results_on_screen(
    sweep, tmp_path, capsys, monkeypatch
):
    target = tmp_path / "x.csv"
    _answer(sweep, "192.0.2.39")
    real = sweep["replies"]

    def ping_then_take_the_file(hosts, **kw):
        target.write_text("someone else", encoding="utf-8")
        return {h: real.get(h, PingStats(target=h, sent=2, received=0)) for h in hosts}

    monkeypatch.setattr(scan_mod, "ping_many", ping_then_take_the_file)
    code = _scan("192.0.2.39", "--save", str(target))
    out, err = capsys.readouterr()
    assert code == 4, err
    assert "192.0.2.39" in out
    assert "could not save the device list" in err
    assert target.read_text(encoding="utf-8") == "someone else"


def test_open_ports_found_with_p_reach_the_saved_ledger(sweep, tmp_path):
    _answer(sweep, "192.0.2.39")
    sweep["ports"] = {502: "open"}
    path = tmp_path / "new.csv"
    assert _scan("192.0.2.39", "-p", "modbus", "-p", "4840", "--save", str(path)) in (
        0,
        None,
    )
    rows = path.read_text(encoding="utf-8-sig").splitlines()
    assert rows[1].split(",")[:3] == ["", "192.0.2.39", "502"]


def test_the_same_address_on_two_nics_is_not_an_ip_conflict(sweep, capsys):
    _answer(sweep, "192.0.2.1")
    sweep["table"] = [
        Neighbor(
            "192.0.2.1", mac="00:00:5e:00:53:01", state="reachable", interface="eth0"
        ),
        Neighbor(
            "192.0.2.1", mac="00:00:5e:00:53:02", state="reachable", interface="wlan0"
        ),
    ]
    _scan("192.0.2.1")
    assert "IP conflict" not in capsys.readouterr().out


def test_two_answers_to_one_ping_are_an_ip_conflict(sweep, capsys):
    _answer(sweep, "192.0.2.1", dups=2)
    _scan("192.0.2.1")
    assert "possible IP conflict: 192.0.2.1" in capsys.readouterr().out


IEEE_OUI = (
    "OUI/MA-L                                                    Organization\n"
    "company_id                                                  Organization\n"
    "                                                            Address\n"
    "\n"
    "00-30-DE   (hex)\t\tWAGO Kontakttechnik GmbH\n"
    "0030DE     (base 16)\t\tWAGO Kontakttechnik GmbH\n"
    "\t\t\t\tHansastrasse 27\n"
    "\t\t\t\t100084  Minden\n"
    "\t\t\t\tDE\n"
    "\n"
    "70-B3-D5   (hex)\t\tIEEE Registration Authority\n"
    "70B3D5     (base 16)\t\tIEEE Registration Authority\n"
    "\t\t\t\t445 Hoes Lane\n"
)


def test_the_vendor_table_reads_makers_not_postcodes(tmp_path, monkeypatch):
    path = tmp_path / "oui.txt"
    path.write_text(IEEE_OUI, encoding="utf-8")
    monkeypatch.setenv("NETDIAG_OUI_FILE", str(path))
    oui.reset()
    assert oui.parse_oui_text(IEEE_OUI) == {
        "0030de": "WAGO Kontakttechnik GmbH",
        "70b3d5": "IEEE Registration Authority",
    }
    assert oui.lookup("0030.de00.0001") == "WAGO Kontakttechnik GmbH"
    assert oui.lookup("0:30:de:0:0:1") == "WAGO Kontakttechnik GmbH"
    assert oui.lookup("70:b3:d5:12:34:56") is None  # an MA-S block: maker unknown
