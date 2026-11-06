"""Device lists ("台帳"): Excel CSV (Shift_JIS / UTF-8 BOM / UTF-16), YAML,
JSON, TOML, Japanese or English headers, header-less lists, protocol names."""

import json

import pytest

from netdiag.inventory import (
    Device,
    InventoryError,
    devices_to_csv,
    load_devices,
    parse_csv_text,
    parse_ports,
    parse_structured,
    parse_target_spec,
)


def test_parse_ports_numbers_and_protocol_names():
    assert parse_ports("502") == [502]
    assert parse_ports("502;4840") == [502, 4840]
    assert parse_ports("modbus, opcua") == [502, 4840]
    assert parse_ports("modbus・enip") == [502, 44818]
    assert parse_ports([502, "melsoft", "502"]) == [502, 5007]
    assert parse_ports("502/tcp") == [502]
    assert parse_ports(None) == [] and parse_ports("") == []
    with pytest.raises(InventoryError):
        parse_ports("profinet-io")
    with pytest.raises(InventoryError):
        parse_ports("70000")


@pytest.mark.parametrize(
    "spec, name, host, ports",
    [
        ("192.168.3.39", "192.168.3.39", "192.168.3.39", []),
        ("192.168.3.39:502", "192.168.3.39", "192.168.3.39", [502]),
        ("192.168.3.39:502,4840", "192.168.3.39", "192.168.3.39", [502, 4840]),
        ("PLC1=192.168.3.39:modbus", "PLC1", "192.168.3.39", [502]),
        ("[fe80::1]:502", "fe80::1", "fe80::1", [502]),
        ("fe80::1", "fe80::1", "fe80::1", []),
        ("plc1.line.local", "plc1.line.local", "plc1.line.local", []),
    ],
)
def test_parse_target_spec(spec, name, host, ports):
    d = parse_target_spec(spec)
    assert (d.name, d.host, d.ports) == (name, host, ports)


def test_csv_shift_jis_with_japanese_headers(tmp_path):
    text = (
        "名前,IPアドレス,ポート,ライン,備考,MACアドレス\r\n"
        "PLC-1,192.168.3.39,502;5007,1ライン,メインPLC,00-26-92-AA-BB-CC\r\n"
        "HMI-1,192.168.3.40,vnc,1ライン,,\r\n"
        "\r\n"
        "# コメント行\r\n"
        "ロボット,192.168.3.41,,2ライン,,\r\n"
    )
    path = tmp_path / "台帳.csv"
    path.write_bytes(text.encode("cp932"))  # what Japanese Excel saves
    devs = load_devices(path)
    assert [d.name for d in devs] == ["PLC-1", "HMI-1", "ロボット"]
    assert devs[0].ports == [502, 5007]
    assert devs[0].group == "1ライン" and devs[0].note == "メインPLC"
    assert devs[0].mac == "00-26-92-AA-BB-CC"
    assert devs[1].ports == [5900]
    assert devs[2].ports == []


def test_headers_match_whatever_the_case_spacing_or_brackets():
    csv_devs = parse_csv_text(
        "名前,IP Address,RTT Warn (ms),ロス上限（%）,MAC_Address\n"
        "PLC,192.168.3.39,5,1.5,00-26-92-AA-BB-CC\n"
    )
    dict_devs = parse_structured(
        [{"Name": "PLC", "IP_Address": "192.168.3.39", "RTT Warn (ms)": 5}]
    )
    for d in csv_devs + dict_devs:
        assert (d.name, d.host, d.rtt_warn_ms) == ("PLC", "192.168.3.39", 5.0)
    assert (csv_devs[0].loss_warn_pct, csv_devs[0].mac) == (1.5, "00-26-92-AA-BB-CC")


def test_csv_utf8_bom_tab_separated(tmp_path):
    path = tmp_path / "devices.tsv"
    path.write_bytes("name\thost\tports\nPLC\t10.0.0.5\t502\n".encode("utf-8-sig"))
    assert load_devices(path)[0].host == "10.0.0.5"


def test_csv_utf16_unicode_text(tmp_path):
    path = tmp_path / "devices.txt"
    path.write_bytes("機器名\tIP\nPLC\t10.0.0.5\n".encode("utf-16"))
    assert load_devices(path)[0].name == "PLC"


def test_headerless_list_finds_the_ip_column():
    devs = parse_csv_text("192.168.3.39\nPLC-2,192.168.3.40,502\n192.168.3.41:44818\n")
    assert [(d.name, d.host, d.ports) for d in devs] == [
        ("192.168.3.39", "192.168.3.39", []),
        ("PLC-2", "192.168.3.40", [502]),
        ("192.168.3.41", "192.168.3.41", [44818]),
    ]


def test_csv_errors_name_the_line():
    with pytest.raises(InventoryError, match="line 3"):
        parse_csv_text("name,host,ports\nA,10.0.0.1,502\nB,10.0.0.2,bogus\n")
    with pytest.raises(InventoryError, match="line 2"):
        parse_csv_text("name,host\nA,\n")
    with pytest.raises(InventoryError):
        parse_csv_text("# only a comment\n")


def test_host_column_may_carry_a_port():
    devs = parse_csv_text("name,ip\nPLC,192.168.3.39:502\n")
    assert (devs[0].host, devs[0].ports) == ("192.168.3.39", [502])


def test_structured_formats_and_defaults(tmp_path):
    data = {
        "defaults": {"ports": "modbus"},
        "devices": [
            "192.168.3.50",
            {
                "name": "PLC",
                "host": "192.168.3.39",
                "ports": [502, 5007],
                "rtt_warn_ms": 5,
            },
            {"名前": "HMI", "IPアドレス": "192.168.3.40"},
        ],
    }
    devs = parse_structured(data)
    assert devs[0].ports == [502]  # defaults apply to bare entries
    assert devs[1].rtt_warn_ms == 5.0 and devs[1].ports == [502, 5007]
    assert (devs[2].name, devs[2].ports) == ("HMI", [502])

    p = tmp_path / "line.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert len(load_devices(p)) == 3


def test_network_checker_hosts_toml_is_a_device_list(tmp_path):
    p = tmp_path / "hosts.toml"
    p.write_text(
        '[[host]]\nname = "192.168.3.39"\ninterval = 30\nrtt_warn_ms = 200\n',
        encoding="utf-8",
    )
    d = load_devices(p)[0]
    assert (d.host, d.interval, d.rtt_warn_ms) == ("192.168.3.39", 30, 200.0)


def test_yaml_device_list(tmp_path):
    pytest.importorskip("yaml")
    p = tmp_path / "line1.yaml"
    p.write_text(
        "devices:\n  - {name: PLC, host: 192.168.3.39, ports: modbus}\n",
        encoding="utf-8",
    )
    assert load_devices(p)[0].ports == [502]


def test_devices_to_csv_roundtrip():
    devs = [
        Device(
            name="PLC",
            host="192.168.3.39",
            ports=[502, 4840],
            group="L1",
            note="メイン",
        )
    ]
    back = parse_csv_text(devices_to_csv(devs))
    assert (back[0].name, back[0].host, back[0].ports, back[0].group, back[0].note) == (
        "PLC",
        "192.168.3.39",
        [502, 4840],
        "L1",
        "メイン",
    )


def test_csv_output_is_safe_for_excel_and_round_trips():
    devs = [Device(name="=HYPERLINK(1)", host="10.0.0.1", note="-note", group="@g")]
    text = devices_to_csv(devs)
    assert "'=HYPERLINK(1)" in text and "'-note" in text and "'@g" in text
    back = parse_csv_text(text)[0]
    assert (back.name, back.note, back.group) == ("=HYPERLINK(1)", "-note", "@g")
